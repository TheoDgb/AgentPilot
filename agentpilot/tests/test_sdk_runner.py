"""Lancement de l'agent Support par le Claude Agent SDK, avec un faux SDK (aucun appel réel)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLINotFoundError, ProcessError

from agentpilot import sdk_runner
from agentpilot.hooks import SupportHooks
from agentpilot.sdk_runner import check_context
from agentpilot.support_agent import (
    ACTION_TOOL_NAMES,
    REPO_ROOT,
    SUPPORT_TOOL_NAMES,
    build_system_prompt,
    expected_context,
)
from agentpilot.tests.conftest import (
    INIT_TOOLS,
    MODEL,
    TODAY,
    FakeQuery,
    MemoryJournal,
    init_message,
    result_message,
    tool_result,
    tool_use,
)

pytestmark = pytest.mark.anyio

ENVIRON = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    # Ne doivent jamais atteindre l'agent ni le serveur MCP
    "CHAOS_TOKEN": "chaos-secret",
    "SHOPSIM_MCP_ACTIONS": "1",
}
DECISION = {
    "ticket_id": 21,
    "category": "refund_request",
    "action": "refuse",
    "requires_human_approval": False,
    "customer_reply": "Bonjour, le délai de remboursement est dépassé.\n\nLe service client Lumen",
    "facts_used": [{"tool": "get_shipment", "field": "days_since_delivery", "value": 46}],
}


async def decide_ticket(ticket_id: int, **kwargs: Any) -> sdk_runner.AgentResult:  # noqa: ANN401
    return await sdk_runner.decide_ticket(ticket_id, journal=MemoryJournal(), **kwargs)


def happy_path(output: Any = DECISION) -> FakeQuery:  # noqa: ANN401
    return FakeQuery(
        init_message(),
        tool_use("t1", "mcp__shopsim__get_ticket", ticket_id=21),
        tool_result("t1"),
        tool_use("t2", "mcp__shopsim__get_payment", order_id=999),
        tool_result("t2", is_error=True),
        tool_use("t3", "StructuredOutput", **(output or {})),
        tool_result("t3"),
        result_message(output),
    )


# --- Options : isolement ---------------------------------------------------------------


async def test_options_isolate_the_agent() -> None:
    fake = happy_path()

    await decide_ticket(21, environ=ENVIRON, query=fake)

    options = fake.options
    assert options is not None
    assert fake.prompt == "Traite le ticket n°21. Rends ta décision au format demandé."
    # Aucun réglage, skill, plugin ni CLAUDE.md du disque
    assert options.setting_sources == []
    assert options.skills == []
    assert options.plugins == []
    assert options.strict_mcp_config is True
    # Consigne de l'agent seule (remplace celle de Claude Code), avec la date de l'horloge
    assert options.system_prompt == build_system_prompt(TODAY)
    assert "Aujourd'hui, nous sommes le 2026-07-06." in options.system_prompt
    # Aucun outil intégré ; seuls les outils shopsim (8 de lecture, 5 d'action)
    assert options.tools == []
    assert options.allowed_tools == sorted(SUPPORT_TOOL_NAMES)
    assert len(options.allowed_tools) == 13
    # Les actions passent toujours par les hooks : avant (validation) et après (journal)
    assert options.hooks is not None
    assert set(options.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    pre = options.hooks["PreToolUse"][0]
    assert pre.matcher is None
    assert isinstance(pre.hooks[0].__self__, SupportHooks)  # type: ignore[attr-defined]
    assert pre.hooks[0].__self__.ticket_id == 21  # type: ignore[attr-defined]
    assert {"Bash", "Read", "WebFetch", "Agent", "Skill"} <= set(options.disallowed_tools)
    assert "StructuredOutput" not in options.disallowed_tools
    assert options.permission_mode == "dontAsk"
    assert options.output_format is not None
    assert options.output_format["schema"]["title"] == "SupportDecision"
    assert options.max_turns == sdk_runner.MAX_TURNS
    assert options.max_budget_usd == sdk_runner.MAX_BUDGET_USD


async def test_options_use_empty_temporary_dirs_and_mcp_with_actions() -> None:
    fake = happy_path()

    await decide_ticket(21, environ=ENVIRON, query=fake)

    options = fake.options
    assert options is not None
    cwd = Path(str(options.cwd))
    assert not cwd.is_relative_to(REPO_ROOT)
    # Supprimés après l'exécution
    assert not cwd.exists()
    assert not Path(options.env["CLAUDE_CONFIG_DIR"]).exists()
    assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert options.env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    server = options.mcp_servers["shopsim"]  # type: ignore[index]
    assert server["command"] == str(REPO_ROOT / ".venv" / "bin" / "python")
    # Actions ouvertes pour le serveur MCP de cet agent seulement (valeur exacte)
    assert server["env"]["SHOPSIM_MCP_ACTIONS"] == "1"
    assert "SHOPSIM_MCP_ACTIONS" not in options.env
    # Aucun secret transmis
    everything = repr(options)
    assert "chaos-secret" not in everything
    assert "CHAOS_TOKEN" not in everything


# --- Réussite --------------------------------------------------------------------------


async def test_decision_is_validated_with_metrics() -> None:
    result = await decide_ticket(21, environ=ENVIRON, query=happy_path())

    assert result.error is None
    assert result.output == DECISION | {"amount_cents": None, "escalation_reason": None}
    assert result.simulation_date == TODAY
    assert result.model == MODEL
    assert result.total_cost_usd == 0.0217
    assert (result.duration_ms, result.num_turns) == (8_000, 4)
    assert result.session_id == "session-test"
    assert result.available_tools == INIT_TOOLS


# --- Échecs : enregistrés avec ce qui a été mesuré -------------------------------------


@pytest.mark.parametrize(
    ("messages", "message"),
    [
        ((init_message([*INIT_TOOLS, "Bash"]), result_message(DECISION)), "interdits : Bash"),
        (
            (init_message([*INIT_TOOLS, "mcp__shopsim__drop_orders"]), result_message(DECISION)),
            "interdits : mcp__shopsim__drop_orders",
        ),
        (
            (
                init_message([t for t in INIT_TOOLS if t != "mcp__shopsim__refund_order"]),
                result_message(DECISION),
            ),
            "manquants : mcp__shopsim__refund_order",
        ),
        ((init_message(status="failed"), result_message(DECISION)), "non connecté (failed)"),
        ((result_message(DECISION),), "sans événement d'initialisation"),
        ((init_message(),), "sans résultat final"),
        ((init_message(), result_message(None)), "pas rendu de sortie structurée"),
        (
            (init_message(), result_message(None, subtype="error_max_turns", is_error=True)),
            "a échoué : error_max_turns",
        ),
        ((init_message(), result_message(DECISION | {"action": "refund"})), "amount_cents est"),
        ((init_message(), result_message(DECISION | {"ticket_id": 22})), "ticket 22 au lieu de 21"),
    ],
)
async def test_bad_runs_are_failures(messages: tuple[Any, ...], message: str) -> None:
    result = await decide_ticket(21, environ=ENVIRON, query=FakeQuery(*messages))

    assert result.error is not None and message in result.error
    assert result.output is None


async def test_forbidden_tool_stops_the_run_before_any_decision() -> None:
    fake = FakeQuery(init_message([*INIT_TOOLS, "Bash"]), tool_use("t1", "Bash", command="ls"))

    result = await decide_ticket(21, environ=ENVIRON, query=fake)

    assert "interdits : Bash" in (result.error or "")
    assert result.output is None


async def test_failed_result_keeps_its_cost() -> None:
    fake = FakeQuery(
        init_message(),
        result_message(None, subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.5),
        # Le SDK lève après un résultat en erreur : le motif lu est gardé
        error=ProcessError("Command failed", exit_code=1),
    )

    result = await decide_ticket(21, environ=ENVIRON, query=fake)

    assert result.error == "L'agent a échoué : error_max_budget_usd"
    assert result.total_cost_usd == 0.5


async def test_sdk_error_is_readable() -> None:
    fake = FakeQuery(error=CLINotFoundError("Claude Code not found"))

    result = await decide_ticket(21, environ=ENVIRON, query=fake)

    assert result.error is not None
    assert result.error.startswith("Erreur du Claude Agent SDK")


async def test_timeout_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Slow(FakeQuery):
        async def _stream(self) -> Any:  # noqa: ANN401
            yield init_message()
            await asyncio.sleep(5)

    result = await sdk_runner.run_agent(
        "x",
        sdk_runner.SupportDecision,
        TODAY,
        hooks=SupportHooks(21, MemoryJournal()),
        environ=ENVIRON,
        query=Slow(),
        timeout_s=0.05,
    )

    assert result.error == "L'agent n'a pas répondu en 0.05 s"


async def test_missing_api_key_is_readable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Ni dans l'environnement ni dans un .env
    monkeypatch.setattr(sdk_runner.support_agent, "ENV_FILE", tmp_path / ".env")

    result = await decide_ticket(21, environ={}, query=happy_path())

    assert "ANTHROPIC_API_KEY absente" in (result.error or "")


async def test_unreadable_clock_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def clock_down(url: str) -> None:
        raise sdk_runner.AgentRunError("Horloge de ShopSim illisible")

    monkeypatch.setattr(sdk_runner.support_agent, "simulation_today", clock_down)

    result = await decide_ticket(21, environ=ENVIRON, query=happy_path())

    assert result.error == "Horloge de ShopSim illisible"


# --- Vérification d'isolement ----------------------------------------------------------


async def test_check_passes_with_the_right_answers() -> None:
    answer = expected_context(TODAY).model_dump()
    fake = FakeQuery(init_message(), result_message(answer))

    result = await check_context(journal=MemoryJournal(), environ=ENVIRON, query=fake)

    assert result.error is None
    assert result.output == answer
    assert fake.options is not None
    assert fake.options.output_format["schema"]["title"] == "ContextCheck"  # type: ignore[index]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"chaos_token_header": "X-Chaos-Token"}, "CLAUDE.md développeur"),
        ({"today": "2026-10-26"}, "today = '2026-10-26' au lieu de '2026-07-06'"),
    ],
)
async def test_check_fails_on_wrong_answers(changes: dict[str, Any], message: str) -> None:
    answer = expected_context(TODAY).model_dump() | changes

    result = await check_context(
        journal=MemoryJournal(),
        environ=ENVIRON,
        query=FakeQuery(init_message(), result_message(answer)),
    )

    assert message in (result.error or "")


async def test_check_runs_without_ticket() -> None:
    """Pendant la vérification, les hooks n'ont pas de ticket : toute action est refusée."""
    fake = FakeQuery(init_message(), result_message(expected_context(TODAY).model_dump()))

    await check_context(journal=MemoryJournal(), environ=ENVIRON, query=fake)

    assert fake.options is not None and fake.options.hooks is not None
    hooks = fake.options.hooks["PreToolUse"][0].hooks[0].__self__  # type: ignore[attr-defined]
    assert hooks.ticket_id is None


# --- Jamais d'action sans hooks --------------------------------------------------------


async def test_actions_without_hooks_are_refused() -> None:
    fake = happy_path()

    result = await sdk_runner.run_agent(
        "x", sdk_runner.SupportDecision, TODAY, hooks=None, environ=ENVIRON, query=fake
    )

    assert result.error == "Outils d'action sans hooks de validation : lancement refusé"
    # L'agent n'a pas été lancé
    assert fake.options is None


def test_action_tools_are_the_mcp_action_tools() -> None:
    assert ACTION_TOOL_NAMES == {
        "mcp__shopsim__reply_to_ticket",
        "mcp__shopsim__refund_order",
        "mcp__shopsim__resend_order",
        "mcp__shopsim__escalate_ticket",
        "mcp__shopsim__resolve_ticket",
    }
