"""Juge IA facultatif (J5.2) : ton et clarté des réponses au client, selon `judge_rubric.md`.

Désactivé par défaut (`python -m evals.grade --judge`), jamais compté dans le score : un modèle
n'est pas déterministe. Coût plafonné (`--judge-max-usd`) et affiché.

Le juge ne reçoit que la réponse de l'agent, présentée comme une donnée : jamais le message du
client (les tickets pièges contiennent des injections de consigne), jamais la vérité terrain.
Seule autre donnée : la date du jour vue par le client (date simulée de l'évaluation), sans
laquelle le juge lirait les dates de la réponse à sa propre date et les croirait incohérentes.
Même isolement que l'agent Support : aucun outil, aucun serveur MCP, aucun réglage du disque,
dossier de travail vide, CLI lancée par `claude_guard`.
"""

import hashlib
import os
import tempfile
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentpilot import claude_guard, support_agent
from agentpilot.sdk_runner import BUILTIN_TOOLS, QueryFn
from agentpilot.support_agent import AgentRunError

RUBRIC_PATH = Path(__file__).with_name("judge_rubric.md")
DEFAULT_MODEL = "haiku"
DEFAULT_MAX_USD = Decimal("0.50")
# Plafond d'un appel, réservé avant chaque appel : le plafond total n'est jamais dépassé
CALL_MAX_USD = Decimal("0.05")
MAX_TURNS = 3
TIMEOUT_S = 120


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgeScore(Strict):
    """Sortie structurée du juge."""

    tone: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    justification: str = Field(min_length=1, max_length=500)


class JudgedReply(Strict):
    ticket_id: int
    pass_index: int
    score: JudgeScore | None = None
    error: str | None = None
    cost_usd: float | None = None


class JudgeSummary(Strict):
    model: str
    rubric_sha256: str
    max_usd: float
    cost_usd: float = 0.0
    # Réponses non jugées : plafond atteint
    skipped: int = 0
    items: list[JudgedReply] = []


@dataclass(frozen=True)
class Reply:
    ticket_id: int
    pass_index: int
    text: str


def rubric() -> str:
    return RUBRIC_PATH.read_text(encoding="utf-8")


def rubric_sha256() -> str:
    return hashlib.sha256(RUBRIC_PATH.read_bytes()).hexdigest()


def prompt(text: str, today: date) -> str:
    # Balises neutralisées dans le texte : la réponse ne peut pas fermer son propre cadre
    data = text.replace("<reponse>", "‹reponse›").replace("</reponse>", "‹/reponse›")
    return (
        f"Date du jour pour le client : {today.isoformat()}.\n\n"
        f"Note cette réponse selon la grille.\n\n<reponse>\n{data}\n</reponse>"
    )


def options(workdir: Path, *, model: str, api_key: str, cli: str) -> ClaudeAgentOptions:
    cwd, config_dir = workdir / "cwd", workdir / "claude-config"
    cwd.mkdir()
    config_dir.mkdir()
    return ClaudeAgentOptions(
        cli_path=claude_guard.write_launcher(workdir),
        system_prompt=rubric(),
        setting_sources=[],
        skills=[],
        plugins=[],
        strict_mcp_config=True,
        mcp_servers={},
        tools=[],
        allowed_tools=[],
        disallowed_tools=BUILTIN_TOOLS,
        permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": JudgeScore.model_json_schema()},
        model=model,
        max_turns=MAX_TURNS,
        max_budget_usd=float(CALL_MAX_USD),
        cwd=str(cwd),
        env={
            "ANTHROPIC_API_KEY": api_key,
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            claude_guard.CLI_ENV: cli,
            claude_guard.SERVICE_PID_ENV: str(os.getpid()),
        },
    )


async def judge_one(
    reply: Reply, *, today: date, model: str, api_key: str, cli: str, query: QueryFn
) -> JudgedReply:
    judged = JudgedReply(ticket_id=reply.ticket_id, pass_index=reply.pass_index)
    final: ResultMessage | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="agentpilot-judge-") as workdir:
            opts = options(Path(workdir), model=model, api_key=api_key, cli=cli)
            text = prompt(reply.text, today)
            with anyio.fail_after(TIMEOUT_S):
                async with aclosing(query(prompt=text, options=opts)) as messages:
                    async for message in messages:
                        if isinstance(message, ResultMessage):
                            final = message
    except TimeoutError:
        judged.error = f"pas de réponse en {TIMEOUT_S} s"
    except ClaudeSDKError as exc:
        judged.error = f"erreur du Claude Agent SDK : {type(exc).__name__}"
    if final is None:
        judged.error = judged.error or "aucun résultat"
        return judged
    judged.cost_usd = final.total_cost_usd
    if final.is_error or final.structured_output is None:
        judged.error = judged.error or "pas de note structurée"
        return judged
    try:
        judged.score = JudgeScore.model_validate(final.structured_output)
    except ValidationError:
        judged.error = "note hors grille"
    return judged


async def judge(
    replies: list[Reply],
    *,
    today: date,
    model: str = DEFAULT_MODEL,
    max_usd: Decimal = DEFAULT_MAX_USD,
    query: QueryFn,
    environ: Mapping[str, str] = os.environ,
) -> JudgeSummary:
    """Juge les réponses une à une, tant que le plafond le permet ; `today` : date simulée."""
    summary = JudgeSummary(model=model, rubric_sha256=rubric_sha256(), max_usd=float(max_usd))
    api_key = support_agent.load_api_key(environ)
    cli = claude_guard.real_cli(environ)
    if cli is None:
        raise AgentRunError("CLI Claude Code introuvable (ni celle du SDK, ni dans le PATH)")
    spent = Decimal(0)
    for index, reply in enumerate(replies):
        if spent + CALL_MAX_USD > max_usd:
            summary.skipped = len(replies) - index
            break
        judged = await judge_one(
            reply, today=today, model=model, api_key=api_key, cli=cli, query=query
        )
        # Coût inconnu : compté à son plafond, comme le budget du service
        spent += Decimal(str(judged.cost_usd)) if judged.cost_usd is not None else CALL_MAX_USD
        summary.items.append(judged)
    summary.cost_usd = float(spent)
    return summary
