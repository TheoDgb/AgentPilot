"""Hooks des agents : journal des appels (tous), validation humaine des actions (Support).

`JournalHooks` : ce qui est commun, le journal de chaque appel. `SupportHooks` y ajoute la
validation des actions ; `OpsHooks` ne laisse passer que les outils de lecture de l'agent Ops
(en architecture `subagents`, les outils de chaque agent, et l'outil `Agent` pour les deux seuls
types de subagents prévus).

- `PreToolUse` : une action passe, est refusée, ou est mise en attente d'un humain selon
  `agentpilot/approval.py`. Un refus ou une mise en attente renvoie `deny` à l'agent, avec un
  message clair ; l'appel n'atteint jamais ShopSim. Arrêt d'urgence actif (relu en base à
  chaque action) : toutes les actions sont refusées, les lectures continuent, et l'exécution
  est marquée « proposition seulement ».
- `PostToolUse` / `PostToolUseFailure` : chaque appel exécuté est inscrit au journal.

Pour laisser passer, le hook ne renvoie rien (`{}`), jamais `allow` : un `allow` de hook
court-circuiterait les règles de permission (`dontAsk`, `allowed_tools`), qui restent la
seule porte d'entrée des outils.

Chaque inscription au journal écrit aussi une ligne `tool.called` dans les logs (outil,
issue, commande et montant), jamais les paramètres texte (message au client, motif), et
compte l'appel dans `agentpilot_tool_calls_total` (outil et issue seulement).
"""

import asyncio
import contextlib
import itertools
import json
import logging
from collections import Counter
from collections.abc import Mapping
from typing import Any, Protocol

from claude_agent_sdk import HookContext, HookMatcher
from claude_agent_sdk.types import HookEvent, HookInput, HookJSONOutput

from agentpilot import approval, metrics
from agentpilot.approval import Kind
from agentpilot.journal import CallEntry, Journal, PendingEntry
from agentpilot.models import CallOutcome
from agentpilot.support_agent import ACTION_TOOL_NAMES, READ_TOOL_NAMES, STRUCTURED_OUTPUT_TOOL
from mcp_server.shopsim_client import ShopSimError
from observability.logs import log_event

logger = logging.getLogger(__name__)

# Réponse d'outil gardée au journal (au-delà : texte tronqué)
MAX_RESPONSE_CHARS = 4_000
# Agent Ops : réponses gardées en entier (jusqu'à 50 lignes de logs), pour recouper chaque
# preuve de son rapport avec la vraie réponse (`evals.ops_evidence`)
OPS_MAX_RESPONSE_CHARS = 20_000
# Délai des hooks : les lectures dans ShopSim peuvent subir une panne de latence
HOOK_TIMEOUT_S = 60
EMERGENCY_MESSAGE = (
    "Arrêt d'urgence : aucune action n'est possible pour l'instant. N'écris pas au client et "
    "n'annonce rien comme fait. Ne réessaie aucune action : rends ta décision comme une "
    "proposition."
)


def _deny(message: str) -> HookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def clip(response: Any, limit: int = MAX_RESPONSE_CHARS) -> Any:  # noqa: ANN401
    """Réponse sérialisable en JSON, tronquée si elle est longue.

    Claude Code transmet la réponse d'un outil MCP sous forme de texte JSON : on la décode
    pour qu'elle soit lisible et interrogeable en base. Un texte qui n'est pas du JSON reste
    tel quel.
    """
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            pass
    text = json.dumps(response, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return json.loads(text)
    return {"truncated": text[:limit]}


class ToolHooks(Protocol):
    def matchers(self) -> dict[HookEvent, list[HookMatcher]]: ...


class JournalHooks:
    """Journal des appels d'une exécution : chaque appel exécuté ou refusé y est inscrit.

    `pre_tool_use` est à définir par chaque agent ; `called` garde les outils réellement
    exécutés (réussis ou en échec), pour vérifier les preuves d'un rapport."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._seq = itertools.count(1)
        self.called: set[str] = set()
        self.max_response_chars = MAX_RESPONSE_CHARS

    async def pre_tool_use(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        raise NotImplementedError

    async def _record(self, entry_fields: Mapping[str, Any]) -> None:
        # Numéro attribué dans la boucle : l'ordre des appels est gardé
        entry = CallEntry(seq=next(self._seq), **entry_fields)
        if entry.outcome in (CallOutcome.SUCCEEDED, CallOutcome.FAILED):
            self.called.add(entry.tool)
        await asyncio.to_thread(self.journal.record_call, entry)
        metrics.tool_called(entry.tool, entry.outcome)
        numbers = {
            key: entry.input[key]
            for key in ("order_id", "amount_cents")
            if isinstance(entry.input.get(key), int)
        }
        log_event(
            logger,
            "tool.called",
            logging.INFO if entry.outcome is CallOutcome.SUCCEEDED else logging.WARNING,
            tool=approval.short_name(entry.tool),
            outcome=entry.outcome,
            action_id=entry.pending_action_id,
            duration_ms=entry.duration_ms,
            **numbers,
        )

    async def _refuse(
        self,
        name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None,
        message: str,
        agent: str | None = None,
    ) -> HookJSONOutput:
        await self._record(
            {
                "tool_use_id": tool_use_id,
                "tool": name,
                "input": tool_input,
                "outcome": CallOutcome.DENIED,
                "error": message,
                "agent": agent,
            }
        )
        return _deny(message)

    async def post_tool_use(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        name: str = data["tool_name"]  # type: ignore[typeddict-item]
        if name == STRUCTURED_OUTPUT_TOOL:
            return {}
        failed = data["hook_event_name"] == "PostToolUseFailure"
        await self._record(
            {
                "tool_use_id": tool_use_id or data.get("tool_use_id"),
                "tool": name,
                "input": data["tool_input"],  # type: ignore[typeddict-item]
                "outcome": CallOutcome.FAILED if failed else CallOutcome.SUCCEEDED,
                "response": (
                    None if failed else clip(data.get("tool_response"), self.max_response_chars)
                ),
                "error": data.get("error") if failed else None,
                "duration_ms": data.get("duration_ms"),
                # Type du subagent auteur de l'appel ; absent pour l'agent principal
                "agent": data.get("agent_type"),
            }
        )
        return {}

    def matchers(self) -> dict[HookEvent, list[HookMatcher]]:
        """Pour `ClaudeAgentOptions.hooks` : tous les outils passent par ces hooks."""
        return {
            "PreToolUse": [HookMatcher(hooks=[self.pre_tool_use], timeout=HOOK_TIMEOUT_S)],
            "PostToolUse": [HookMatcher(hooks=[self.post_tool_use], timeout=HOOK_TIMEOUT_S)],
            "PostToolUseFailure": [HookMatcher(hooks=[self.post_tool_use], timeout=HOOK_TIMEOUT_S)],
        }


class SupportHooks(JournalHooks):
    """Hooks de l'agent Support : ticket traité et journal propres à cette exécution."""

    def __init__(self, ticket_id: int | None, journal: Journal) -> None:
        super().__init__(journal)
        self.ticket_id = ticket_id

    async def _emergency_stop(self) -> bool:
        try:
            return await asyncio.to_thread(self.journal.emergency_stop)
        except Exception:  # noqa: BLE001  (base illisible : on ferme, l'action est refusée)
            return True

    async def pre_tool_use(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        try:
            return await self._decide(data, tool_use_id)
        except Exception as exc:  # noqa: BLE001  (on ferme : une erreur du hook ne laisse rien passer)
            return _deny(approval.unverifiable_message(exc))

    async def _decide(self, data: HookInput, tool_use_id: str | None) -> HookJSONOutput:
        name: str = data["tool_name"]  # type: ignore[typeddict-item]
        tool_input: dict[str, Any] = data["tool_input"]  # type: ignore[typeddict-item]
        tool_use_id = tool_use_id or data.get("tool_use_id")  # type: ignore[assignment]
        if name in READ_TOOL_NAMES or name == STRUCTURED_OUTPUT_TOOL:
            return {}
        if name not in ACTION_TOOL_NAMES:
            # Défense en profondeur : les permissions l'empêchent déjà
            return await self._refuse(name, tool_input, tool_use_id, f"Outil {name} interdit.")
        if await self._emergency_stop():
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.journal.mark_propose_only)
            return await self._refuse(name, tool_input, tool_use_id, EMERGENCY_MESSAGE)

        tool = approval.short_name(name)
        facts: approval.OrderFacts | None = None
        verdict = approval.check_ticket(tool, tool_input, self.ticket_id)
        if verdict.kind is Kind.ALLOW:
            try:
                if tool in approval.GATED:
                    facts = await approval.gather_facts(tool, tool_input)
                pending = (
                    await asyncio.to_thread(self.journal.has_pending, self.ticket_id)
                    if tool == approval.RESOLVE and self.ticket_id is not None
                    else False
                )
            except (ShopSimError, ValueError) as exc:
                # On ferme : sans faits vérifiés, rien n'est exécuté ni mis en attente
                return await self._refuse(
                    name, tool_input, tool_use_id, approval.unverifiable_message(exc)
                )
            verdict = approval.evaluate(
                tool,
                tool_input,
                run_ticket_id=self.ticket_id,
                facts=facts,
                pending_on_ticket=pending,
            )

        if verdict.kind is Kind.ALLOW:
            return {}
        if verdict.kind is Kind.DENY:
            return await self._refuse(name, tool_input, tool_use_id, verdict.message)

        # Mise en attente : de quoi exécuter l'action plus tard, avec la même clé
        assert facts is not None and self.ticket_id is not None
        pending_id, created = await asyncio.to_thread(
            self.journal.hold,
            PendingEntry(
                ticket_id=self.ticket_id,
                tool=tool,
                params=dict(tool_input),
                reasons=[str(r) for r in verdict.reasons],
                facts=facts.record() | {"value_cents": verdict.value_cents},
                idempotency_key=approval.idempotency_key(tool, tool_input),
            ),
        )
        message = approval.hold_message(verdict, already_pending=not created)
        await self._record(
            {
                "tool_use_id": tool_use_id,
                "tool": name,
                "input": tool_input,
                "outcome": CallOutcome.HELD,
                "error": message,
                "pending_action_id": pending_id,
            }
        )
        return _deny(message)


class OpsHooks(JournalHooks):
    """Hooks de l'agent Ops : seuls ses outils de lecture (et la sortie structurée) passent ;
    tout autre outil est refusé et inscrit au journal. Défense en profondeur : les permissions
    (`allowed_tools`, `dontAsk`) l'empêchent déjà.

    Architecture `subagents` (`subagents` : outils de chaque type de subagent) : `allowed` sont
    les outils de l'enquêteur principal ; un appel fait dans un subagent (`agent_type` du hook)
    n'a droit qu'aux outils de ce type ; l'outil `Agent` ne lance qu'un type prévu, tout autre
    (agent généraliste, avec fichiers ou shell) est refusé. `delegated` compte les lancements
    réussis par type."""

    def __init__(
        self,
        journal: Journal,
        allowed: frozenset[str],
        subagents: Mapping[str, frozenset[str]] | None = None,
        delegation_tools: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(journal)
        self.allowed = allowed
        self.subagents = dict(subagents or {})
        self.delegation_tools = delegation_tools
        self.delegated: Counter[str] = Counter()
        self.max_response_chars = OPS_MAX_RESPONSE_CHARS

    async def pre_tool_use(
        self, data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        name: str = data["tool_name"]  # type: ignore[typeddict-item]
        agent: str | None = data.get("agent_type")  # type: ignore[assignment]
        try:
            message = self._refusal(name, data.get("tool_input") or {}, agent)  # type: ignore[arg-type]
            if message is None:
                return {}
            return await self._refuse(
                name,
                data["tool_input"],  # type: ignore[typeddict-item]
                tool_use_id or data.get("tool_use_id"),  # type: ignore[arg-type]
                message,
                agent,
            )
        except Exception:  # noqa: BLE001  (journal illisible : on refuse quand même)
            return _deny(f"Outil {name} interdit.")

    def _refusal(self, name: str, tool_input: Mapping[str, Any], agent: str | None) -> str | None:
        """None : l'appel passe ; sinon, le motif du refus."""
        if agent is not None:
            tools = self.subagents.get(agent)
            if tools is None:
                return f"Agent {agent} inconnu : aucun outil ne lui est permis."
            if name in tools:
                return None
            return f"Outil {name} interdit à {agent} : il ne lit que {', '.join(sorted(tools))}."
        if name == STRUCTURED_OUTPUT_TOOL:
            return None
        if name in self.delegation_tools and self.subagents:
            kind = tool_input.get("subagent_type")
            if kind in self.subagents:
                return None
            return (
                f"Type d'agent {kind!r} interdit : seuls {', '.join(sorted(self.subagents))} "
                "peuvent être lancés."
            )
        if name in self.allowed:
            return None
        return f"Outil {name} interdit : l'agent Ops ne fait que lire."

    async def _record(self, entry_fields: Mapping[str, Any]) -> None:
        await super()._record(entry_fields)
        if (
            entry_fields["tool"] in self.delegation_tools
            and entry_fields["outcome"] is CallOutcome.SUCCEEDED
            and entry_fields.get("agent") is None
        ):
            self.delegated[str(entry_fields["input"].get("subagent_type"))] += 1
