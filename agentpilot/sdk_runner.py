"""Lancement de l'agent Support par le Claude Agent SDK.

Même isolement que `run_support.py` (CLI) :
- aucun réglage du disque (`setting_sources=[]`), aucune skill ni plugin, aucun CLAUDE.md :
  la consigne et la politique sont passées dans `system_prompt`, qui remplace celle de
  Claude Code ;
- aucun outil intégré (`tools=[]`, plus une liste d'interdits) : seuls les 8 outils de
  lecture du serveur MCP shopsim, en lecture seule forcée ;
- dossier de travail et configuration de Claude Code vides et temporaires ;
- la liste des outils réellement disponibles est vérifiée à l'initialisation, avant toute
  décision.

Une exécution ne lève pas d'exception pour un échec de l'agent : `AgentResult.error` le dit,
avec ce qui a été mesuré jusque-là (coût, outils appelés), pour être enregistré tel quel.
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import query as sdk_query
from pydantic import BaseModel

from agentpilot import support_agent
from agentpilot.schemas import ContextCheck, SupportDecision
from agentpilot.support_agent import (
    CHECK_PROMPT,
    DEFAULT_SHOPSIM_URL,
    MCP_SERVER,
    READ_TOOL_NAMES,
    STRUCTURED_OUTPUT_TOOL,
    AgentRunError,
)

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


@dataclass
class AgentResult:
    """Ce qu'une exécution a produit et mesuré ; `error` est rempli en cas d'échec."""

    simulation_date: date | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    available_tools: list[str] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def build_options(
    schema: type[BaseModel],
    today: date,
    *,
    api_key: str,
    shopsim_url: str,
    workdir: Path,
    model: str,
) -> ClaudeAgentOptions:
    cwd = workdir / "cwd"
    config_dir = workdir / "claude-config"
    cwd.mkdir()
    config_dir.mkdir()
    return ClaudeAgentOptions(
        # Remplace la consigne de Claude Code : seule la consigne de l'agent est envoyée
        system_prompt=support_agent.build_system_prompt(today),
        setting_sources=[],
        skills=[],
        plugins=[],
        strict_mcp_config=True,
        mcp_servers={MCP_SERVER: support_agent.mcp_server_config(shopsim_url)},
        tools=[],
        allowed_tools=sorted(READ_TOOL_NAMES),
        disallowed_tools=BUILTIN_TOOLS,
        permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": schema.model_json_schema()},
        model=model,
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        # Dossier vide : `dontAsk` laisse lire les fichiers du dossier de travail
        cwd=str(cwd),
        env={
            "ANTHROPIC_API_KEY": api_key,
            # Ni ~/.claude.json ni la mémoire automatique de l'utilisateur
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "SHOPSIM_MCP_ACTIONS": "0",
        },
    )


def _record_final(result: AgentResult, final: ResultMessage) -> None:
    result.session_id = final.session_id
    result.total_cost_usd = final.total_cost_usd
    result.duration_ms = final.duration_ms
    result.num_turns = final.num_turns
    if final.is_error or final.subtype != "success":
        result.error = f"L'agent a échoué : {final.result or final.subtype}"
    elif final.structured_output is None:
        result.error = "L'agent n'a pas rendu de sortie structurée"
    else:
        result.output = final.structured_output


async def _collect(messages: AsyncIterator[Any], result: AgentResult) -> None:
    """Parcourt les messages : contrôle des outils à l'initialisation, outils appelés,
    résultat final."""
    calls: dict[str, dict[str, Any]] = {}
    finished = False
    async for message in messages:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            result.model = message.data.get("model")
            result.available_tools = support_agent.check_tools(
                message.data.get("tools", []), message.data.get("mcp_servers", [])
            )
        elif isinstance(message, AssistantMessage):
            if result.available_tools is None:
                raise AgentRunError("L'agent a répondu avant l'initialisation")
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name != STRUCTURED_OUTPUT_TOOL:
                    call = {"name": block.name, "input": block.input, "is_error": None}
                    calls[block.id] = call
                    result.tool_calls.append(call)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in calls:
                    calls[block.tool_use_id]["is_error"] = bool(block.is_error)
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
    today: date,
    *,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
    timeout_s: float = TIMEOUT_S,
) -> AgentResult:
    """Lance l'agent ; renvoie la sortie structurée brute (non validée) ou l'erreur."""
    result = AgentResult(simulation_date=today)
    try:
        api_key = support_agent.load_api_key(environ)
        shopsim_url = environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL)
        with tempfile.TemporaryDirectory(prefix="agentpilot-") as workdir:
            options = build_options(
                schema,
                today,
                api_key=api_key,
                shopsim_url=shopsim_url,
                workdir=Path(workdir),
                model=model,
            )
            async with asyncio.timeout(timeout_s):
                await _collect(query(prompt=prompt, options=options), result)
    except AgentRunError as exc:
        result.error = str(exc)
    except TimeoutError:
        result.error = f"L'agent n'a pas répondu en {timeout_s:g} s"
    except ClaudeSDKError as exc:
        # Le SDK lève aussi après un résultat en erreur : garder le motif déjà lu
        result.error = result.error or f"Erreur du Claude Agent SDK : {exc}"
    if result.error is not None:
        result.output = None
    return result


async def _today(environ: Mapping[str, str]) -> date:
    shopsim_url = environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL)
    return await asyncio.to_thread(support_agent.simulation_today, shopsim_url)


async def decide_ticket(
    ticket_id: int,
    *,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
) -> AgentResult:
    """Décision de l'agent Support sur un ticket ; `output` est une SupportDecision validée."""
    try:
        today = await _today(environ)
    except AgentRunError as exc:
        return AgentResult(error=str(exc))
    result = await run_agent(
        support_agent.ticket_prompt(ticket_id),
        SupportDecision,
        today,
        environ=environ,
        query=query,
        model=model,
    )
    if result.error is None:
        try:
            decision = support_agent.validate_decision(result.output, ticket_id)
            result.output = decision.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            # Une exécution en échec n'expose jamais de décision
            result.output = None
    return result


async def check_context(
    *,
    environ: Mapping[str, str] = os.environ,
    query: QueryFn = sdk_query,
    model: str = DEFAULT_MODEL,
) -> AgentResult:
    """Preuve d'isolement : consigne, politique et date reçues, rien du CLAUDE.md développeur."""
    try:
        today = await _today(environ)
    except AgentRunError as exc:
        return AgentResult(error=str(exc))
    result = await run_agent(
        CHECK_PROMPT, ContextCheck, today, environ=environ, query=query, model=model
    )
    if result.error is None:
        try:
            answer = support_agent.validate_context(result.output, today)
            result.output = answer.model_dump(mode="json")
        except AgentRunError as exc:
            result.error = str(exc)
            # Une exécution en échec n'expose jamais de décision
            result.output = None
    return result
