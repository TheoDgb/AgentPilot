"""Lancement des agents par le Claude Agent SDK : Support (tickets) et Ops (alertes).

Chaque agent a son profil (`AgentProfile` : consigne, serveur MCP et outils, plafonds) ; le
reste, décrit ici pour Support, vaut pour les deux.

Même isolement que `run_support.py` (CLI) :
- aucun réglage du disque (`setting_sources=[]`), aucune skill ni plugin, aucun CLAUDE.md :
  la consigne et la politique sont passées dans `system_prompt`, qui remplace celle de
  Claude Code ;
- aucun outil intégré (`tools=[]`, plus une liste d'interdits) : seuls les outils du serveur
  MCP shopsim ;
- dossier de travail et configuration de Claude Code vides et temporaires ;
- la liste des outils réellement disponibles est vérifiée à l'initialisation, avant toute
  décision ;
- la CLI est lancée par `agentpilot/claude_guard.py` : elle meurt avec le service, même tué
  brutalement, et le serveur MCP avec elle. Fin, délai dépassé ou annulation : le flux du SDK
  est fermé tout de suite (`aclosing`), ce qui arrête la CLI (stdin fermé, puis SIGTERM, puis
  SIGKILL). Délai anyio par précaution : le SDK prévient qu'une annulation asyncio brute peut
  sauter cette escalade (non observé avec la version 0.2.174, mais rien ne le garantit).

Contrairement à la CLI, l'agent a les outils d'action (8 de lecture + 5 d'action), toujours
sous les hooks de `agentpilot/hooks.py` : validation humaine des actions sensibles et
journal de chaque appel. Sans hooks, le lancement est refusé.

Une exécution ne lève pas d'exception pour un échec de l'agent : `AgentResult.error` le dit,
avec ce qui a été mesuré jusque-là (coût, outils appelés), pour être enregistré tel quel.
`AgentResult.error_type` distingue les fautes de l'agent des erreurs d'infrastructure
(`INFRA_ERRORS` : API Anthropic, ShopSim, configuration, CLI), jamais imputables à l'agent.
"""

import asyncio
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk import query as sdk_query
from pydantic import BaseModel

from agentpilot import claude_guard, ops_agent, support_agent
from agentpilot.agent_profile import AgentProfile
from agentpilot.hooks import OpsHooks, SupportHooks, ToolHooks
from agentpilot.journal import Journal
from agentpilot.ops_agent import AlertBrief
from agentpilot.ops_schemas import IncidentReport, OpsContextCheck
from agentpilot.schemas import ContextCheck, SupportDecision
from agentpilot.support_agent import (
    CHECK_PROMPT,
    DEFAULT_SHOPSIM_URL,
    MCP_SERVER,
    SUPPORT_TOOL_NAMES,
    AgentRunError,
)
from observability import logs, trace

QueryFn = Callable[..., AsyncIterator[Any]]

DEFAULT_MODEL = "sonnet"
MAX_TURNS = 15
MAX_BUDGET_USD = 0.50
TIMEOUT_S = 300
# Outils intégrés retirés explicitement, en plus de `tools=[]` (jamais StructuredOutput)
BUILTIN_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
    "Skill",
    "TodoWrite",
    "ToolSearch",
    "AskUserQuestion",
    "ExitPlanMode",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
]


class ErrorType(StrEnum):
    """Catégorie d'un échec, seule écrite dans les logs : le message (`error`), qui peut citer la
    sortie de l'agent, reste en base."""

    CONFIG = "config"
    SHOPSIM_UNAVAILABLE = "shopsim_unavailable"
    # L'API Anthropic a renvoyé une erreur (limite de dépense, clé refusée, surcharge, panne)
    API_UNAVAILABLE = "api_unavailable"
    AGENT_ERROR = "agent_error"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"
    SDK_ERROR = "sdk_error"
    INTERNAL = "internal"


# Erreurs d'infrastructure : jamais une faute de l'agent. Une évaluation s'arrête dessus
# (`evals.run`) et ne les note jamais (`evals.grade`)
INFRA_ERRORS = frozenset(
    {
        ErrorType.API_UNAVAILABLE,
        ErrorType.SHOPSIM_UNAVAILABLE,
        ErrorType.CONFIG,
        ErrorType.SDK_ERROR,
    }
)
# Erreurs de l'API qui ne passeront pas d'elles-mêmes (`AssistantMessage.error`)
API_FAILURES = frozenset({"authentication_failed", "billing_error", "rate_limit", "server_error"})
# Résultat en erreur d'une CLI qui ne donne pas `api_error_status` (avant 2.1.110)
API_ERROR_TEXT = re.compile(r"^API Error: \d{3}\b")


@dataclass
class AgentResult:
    """Ce qu'une exécution a produit et mesuré ; `error` est rempli en cas d'échec."""

    simulation_date: date | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    error_type: ErrorType | None = None
    model: str | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    available_tools: list[str] | None = None
    # Erreur de l'API signalée en cours d'exécution (`AssistantMessage.error`), et code HTTP de
    # l'appel qui a échoué (`ResultMessage.api_error_status`)
    api_failure: str | None = None
    api_status: int | None = None


def support_profile(today: date, prompt_version: str, shopsim_url: str) -> AgentProfile:
    """Agent Support : consigne de la version demandée, serveur MCP shopsim avec les actions
    (toujours sous hooks), plafonds d'un ticket."""
    return AgentProfile(
        mcp_server=MCP_SERVER,
        # Le serveur MCP reprend la trace de l'exécution et écrit ses logs à côté des nôtres
        mcp_config=support_agent.mcp_server_config(
            shopsim_url, actions=True, trace_id=trace.current_trace_id(), log_dir=logs.log_dir()
        ),
        tool_names=SUPPORT_TOOL_NAMES,
        system_prompt=support_agent.build_system_prompt(today, prompt_version),
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        timeout_s=TIMEOUT_S,
    )


def ops_profile(launched_at: datetime, environ: Mapping[str, str]) -> AgentProfile:
    """Agent Ops : consigne et guide de diagnostic, serveur MCP Ops (5 outils de lecture),
    plafonds d'une enquête."""
    return AgentProfile(
        mcp_server=ops_agent.MCP_SERVER,
        mcp_config=ops_agent.mcp_server_config(
            environ, trace_id=trace.current_trace_id(), log_dir=logs.log_dir()
        ),
        tool_names=ops_agent.OPS_TOOL_NAMES,
        system_prompt=ops_agent.build_system_prompt(launched_at),
        max_turns=ops_agent.MAX_TURNS,
        max_budget_usd=ops_agent.MAX_BUDGET_USD,
        timeout_s=ops_agent.TIMEOUT_S,
    )


def build_options(
    schema: type[BaseModel],
    today: date | None,
    *,
    api_key: str,
    shopsim_url: str,
    workdir: Path,
    model: str,
    hooks: ToolHooks,
    cli: str,
    prompt_version: str = support_agent.DEFAULT_PROMPT,
    profile: AgentProfile | None = None,
) -> ClaudeAgentOptions:
    """Options du SDK : même isolement pour tous les agents ; `profile` (Support par défaut)
    donne la consigne, le serveur MCP, les outils et les plafonds."""
    if profile is None:
        assert today is not None
        profile = support_profile(today, prompt_version, shopsim_url)
    cwd = workdir / "cwd"
    config_dir = workdir / "claude-config"
    cwd.mkdir()
    config_dir.mkdir()
    return ClaudeAgentOptions(
        # Lanceur qui lie la vie de la CLI à celle du service, puis exécute `cli`
        cli_path=claude_guard.write_launcher(workdir),
        # Remplace la consigne de Claude Code : seule la consigne de l'agent est envoyée
        system_prompt=profile.system_prompt,
        setting_sources=[],
        skills=[],
        plugins=[],
        strict_mcp_config=True,
        mcp_servers={profile.mcp_server: profile.mcp_config},
        tools=[],
        allowed_tools=sorted(profile.tool_names),
        disallowed_tools=BUILTIN_TOOLS,
        permission_mode="dontAsk",
        # Avant chaque outil : validation des actions ; après : journal
        hooks=hooks.matchers(),
        output_format={"type": "json_schema", "schema": schema.model_json_schema()},
        model=model,
        max_turns=profile.max_turns,
        max_budget_usd=profile.max_budget_usd,
        # Dossier vide : `dontAsk` laisse lire les fichiers du dossier de travail
        cwd=str(cwd),
        env={
            "ANTHROPIC_API_KEY": api_key,
            # Ni ~/.claude.json ni la mémoire automatique de l'utilisateur
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            claude_guard.CLI_ENV: cli,
            claude_guard.SERVICE_PID_ENV: str(os.getpid()),
        },
    )


def api_error(result: AgentResult, final: ResultMessage) -> bool:
    """Échec venu de l'API Anthropic, pas de l'agent : toute erreur renvoyée par l'API (la CLI a
    déjà réessayé les erreurs passagères). La limite de dépense répond par exemple en HTTP 400."""
    return (
        final.api_error_status is not None
        or result.api_failure in API_FAILURES
        or API_ERROR_TEXT.match(final.result or "") is not None
    )


def _record_final(result: AgentResult, final: ResultMessage) -> None:
    result.session_id = final.session_id
    result.total_cost_usd = final.total_cost_usd
    result.duration_ms = final.duration_ms
    result.num_turns = final.num_turns
    result.api_status = final.api_error_status
    if final.is_error or final.subtype != "success":
        result.error = f"L'agent a échoué : {final.result or final.subtype}"
        result.error_type = (
            ErrorType.API_UNAVAILABLE if api_error(result, final) else ErrorType.AGENT_ERROR
        )
    elif final.structured_output is None:
        result.error = "L'agent n'a pas rendu de sortie structurée"
        result.error_type = ErrorType.INVALID_OUTPUT
    else:
        result.output = final.structured_output


async def _collect(
    messages: AsyncIterator[Any], result: AgentResult, profile: AgentProfile
) -> None:
    """Parcourt les messages : contrôle des outils à l'initialisation, résultat final.
    Les appels d'outils sont journalisés par les hooks."""
    finished = False
    async for message in messages:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            result.model = message.data.get("model")
            result.available_tools = support_agent.check_tools(
                message.data.get("tools", []),
                message.data.get("mcp_servers", []),
                profile.tool_names,
                server=profile.mcp_server,
            )
        elif isinstance(message, AssistantMessage):
            if message.error is not None:
                result.api_failure = message.error
            if result.available_tools is None and message.error is None:
                raise AgentRunError("L'agent a répondu avant l'initialisation")
        elif isinstance(message, ResultMessage):
            _record_final(result, message)
            finished = True
    if result.available_tools is None:
        raise AgentRunError("Sortie de l'agent sans événement d'initialisation")
    if not finished:
        raise AgentRunError("Sortie de l'agent sans résultat final")


async def run_agent(
    prompt: str,
    schema: type[BaseModel],
    today: date | None,
    *,
    hooks: ToolHooks | None,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
    timeout_s: float | None = None,
    prompt_version: str = support_agent.DEFAULT_PROMPT,
    profile: AgentProfile | None = None,
) -> AgentResult:
    """Lance l'agent (Support par défaut, ou `profile`) ; renvoie la sortie structurée brute
    (non validée) ou l'erreur. `timeout_s` : celui du profil par défaut."""
    result = AgentResult(simulation_date=today)
    if hooks is None:
        result.error = "Outils d'action sans hooks de validation : lancement refusé"
        result.error_type = ErrorType.CONFIG
        return result
    try:
        api_key = support_agent.load_api_key(environ)
        cli = claude_guard.real_cli(environ)
    except AgentRunError as exc:
        result.error, result.error_type = str(exc), ErrorType.CONFIG
        return result
    if cli is None:
        result.error = "CLI Claude Code introuvable (ni celle du SDK, ni dans le PATH)"
        result.error_type = ErrorType.CONFIG
        return result
    try:
        shopsim_url = environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL)
        if profile is None:
            assert today is not None
            profile = support_profile(today, prompt_version, shopsim_url)
        timeout_s = profile.timeout_s if timeout_s is None else timeout_s
        with tempfile.TemporaryDirectory(prefix="agentpilot-") as workdir:
            options = build_options(
                schema,
                today,
                api_key=api_key,
                shopsim_url=shopsim_url,
                workdir=Path(workdir),
                model=model,
                hooks=hooks,
                cli=cli,
                prompt_version=prompt_version,
                profile=profile,
            )
            with anyio.fail_after(timeout_s):
                async with aclosing(query(prompt=prompt, options=options)) as messages:
                    await _collect(messages, result, profile)
    except AgentRunError as exc:
        result.error = str(exc)
        result.error_type = ErrorType.AGENT_ERROR
    except TimeoutError:
        result.error = f"L'agent n'a pas répondu en {timeout_s:g} s"
        result.error_type = ErrorType.TIMEOUT
    except ClaudeSDKError as exc:
        # Le SDK lève aussi après un résultat en erreur : garder le motif déjà lu
        result.error = result.error or f"Erreur du Claude Agent SDK : {exc}"
        result.error_type = result.error_type or ErrorType.SDK_ERROR
    if result.error is not None:
        result.output = None
    return result


async def _today(environ: Mapping[str, str]) -> date:
    shopsim_url = environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL)
    return await asyncio.to_thread(support_agent.simulation_today, shopsim_url)


async def decide_ticket(
    ticket_id: int,
    *,
    journal: Journal,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
    done: Sequence[str] = (),
    prompt_version: str = support_agent.DEFAULT_PROMPT,
) -> AgentResult:
    """Décision de l'agent Support sur un ticket ; `output` est une SupportDecision validée.
    `done` : actions déjà faites par une exécution précédente (reprise) ; `prompt_version` :
    version de la consigne (`support_agent.PROMPTS`)."""
    try:
        today = await _today(environ)
    except AgentRunError as exc:
        return AgentResult(error=str(exc), error_type=ErrorType.SHOPSIM_UNAVAILABLE)
    result = await run_agent(
        support_agent.ticket_prompt(ticket_id, done),
        SupportDecision,
        today,
        hooks=SupportHooks(ticket_id, journal),
        environ=environ,
        query=query,
        model=model,
        prompt_version=prompt_version,
    )
    if result.error is None:
        try:
            decision = support_agent.validate_decision(result.output, ticket_id)
            result.output = decision.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            result.error_type = ErrorType.INVALID_OUTPUT
            # Une exécution en échec n'expose jamais de décision
            result.output = None
    return result


async def check_context(
    *,
    journal: Journal,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
    prompt_version: str = support_agent.DEFAULT_PROMPT,
) -> AgentResult:
    """Preuve d'isolement : consigne, politique et date reçues, rien du CLAUDE.md développeur.
    Sans ticket : les hooks refusent toute action."""
    try:
        today = await _today(environ)
    except AgentRunError as exc:
        return AgentResult(error=str(exc), error_type=ErrorType.SHOPSIM_UNAVAILABLE)
    result = await run_agent(
        CHECK_PROMPT,
        ContextCheck,
        today,
        hooks=SupportHooks(None, journal),
        environ=environ,
        query=query,
        model=model,
        prompt_version=prompt_version,
    )
    if result.error is None:
        try:
            answer = support_agent.validate_context(result.output, today)
            result.output = answer.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            result.error_type = ErrorType.INVALID_OUTPUT
            # Une exécution en échec n'expose jamais de décision
            result.output = None
    return result


# --- Agent Ops -----------------------------------------------------------------------------


async def investigate_alert(
    alert: AlertBrief,
    *,
    journal: Journal,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
) -> AgentResult:
    """Enquête de l'agent Ops sur une alerte ; `output` est un IncidentReport validé (schéma,
    alerte recopiée, preuves tirées d'outils réellement appelés)."""
    launched_at = ops_agent.now()
    hooks = OpsHooks(journal, ops_agent.OPS_TOOL_NAMES)
    result = await run_agent(
        ops_agent.alert_prompt(alert, launched_at),
        IncidentReport,
        None,
        hooks=hooks,
        environ=environ,
        query=query,
        model=model,
        profile=ops_profile(launched_at, environ),
    )
    if result.error is None:
        try:
            report = ops_agent.validate_report(result.output, alert, hooks.called)
            result.output = report.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            result.error_type = ErrorType.INVALID_OUTPUT
            result.output = None
    return result


async def check_ops_context(
    *,
    journal: Journal,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
) -> AgentResult:
    """Preuve d'isolement de l'agent Ops : sa consigne arrive, rien d'autre (questions pièges)."""
    launched_at = ops_agent.now()
    result = await run_agent(
        ops_agent.CHECK_PROMPT,
        OpsContextCheck,
        None,
        hooks=OpsHooks(journal, ops_agent.OPS_TOOL_NAMES),
        environ=environ,
        query=query,
        model=model,
        profile=ops_profile(launched_at, environ),
    )
    if result.error is None:
        try:
            answer = ops_agent.validate_context(result.output, launched_at)
            result.output = answer.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            result.error_type = ErrorType.INVALID_OUTPUT
            result.output = None
    return result
