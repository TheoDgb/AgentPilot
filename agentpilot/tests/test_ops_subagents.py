"""Agent Ops en architecture `subagents` (J6.4) avec un faux SDK : réglage, options du SDK,
consignes, hooks par agent (l'outil `Agent` ne lance que les deux subagents prévus), journal
de l'auteur de chaque appel, vérification d'isolement. Aucun appel réel, aucune base."""

from pathlib import Path
from typing import Any

import pytest

from agentpilot import ops_agent, sdk_runner
from agentpilot.ops_agent import (
    LEAD_TOOL_NAMES,
    OPS_TOOL_NAMES,
    SUBAGENT_TOOLS,
    SUBAGENTS_DIR,
    Architecture,
)
from agentpilot.sdk_runner import BUILTIN_TOOLS, ErrorType
from agentpilot.tests.conftest import (
    Call,
    FakeQuery,
    HookDrivingQuery,
    MemoryJournal,
    result_message,
)
from agentpilot.tests.test_ops_agent import (  # noqa: F401  (ops_sources : fixture)
    ALERT,
    ENVIRON,
    NOW,
    OPS_INIT_TOOLS,
    context,
    ops_call,
    ops_init,
    ops_sources,
    report,
    run_ops_tool,
)
from shopsim.app.models import ChaosScenario

# Sources simulées des outils Ops (logs, Prometheus, horloge) pour tous les tests
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("ops_sources")]

SUBAGENTS = ENVIRON | {"AGENTPILOT_OPS_ARCHITECTURE": "subagents"}
LOGS, METRICS = "logs-investigator", "metrics-investigator"
SUBAGENT_FILES = sorted(SUBAGENTS_DIR.glob("*.md"))
# Outils de fichiers, de shell et de délégation : jamais pour un subagent
NEVER_FOR_A_SUBAGENT = {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "NotebookEdit", "Agent"}


def lead_init() -> Any:  # noqa: ANN401
    return ops_init([*OPS_INIT_TOOLS, "Agent"])


async def run_tool(name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
    """Outils Ops réels ; un lancement de subagent rend son texte, comme la CLI."""
    if name in ("Agent", "Task"):
        return [{"type": "text", "text": f"Constats de {arguments.get('subagent_type')}."}]
    return await run_ops_tool(name, arguments)


def delegate(kind: str | None, tool: str = "Agent") -> Call:
    arguments = {"description": "Enquête", "prompt": "Regarde la route /orders/{order_id}."}
    return Call(tool, arguments | ({"subagent_type": kind} if kind is not None else {}))


def by(agent: str, name: str, **arguments: Any) -> Call:  # noqa: ANN401
    call = ops_call(name, **arguments)
    call.agent = agent
    return call


def delegated_investigation() -> list[Call]:
    return [
        delegate(LOGS),
        by(LOGS, "summarize_logs", service="shopsim", since_minutes=5),
        delegate(METRICS),
        by(METRICS, "query_metrics", metric="shopsim_error_rate", window="1m"),
    ]


async def investigate(
    *calls: Call,
    journal: MemoryJournal | None = None,
    output: Any = None,  # noqa: ANN401
) -> sdk_runner.AgentResult:
    fake = HookDrivingQuery(*calls, output=output or report(), executor=run_tool, init=lead_init())
    return await sdk_runner.investigate_alert(
        ALERT, journal=journal or MemoryJournal(), environ=SUBAGENTS, query=fake
    )


# --- Réglage ------------------------------------------------------------------------------


def test_architecture_setting() -> None:
    assert ops_agent.architecture({}) is Architecture.SOLO
    assert ops_agent.architecture(SUBAGENTS) is Architecture.SUBAGENTS
    with pytest.raises(ValueError, match="inconnue"):
        ops_agent.architecture({"AGENTPILOT_OPS_ARCHITECTURE": "swarm"})


async def test_unknown_architecture_runs_nothing() -> None:
    fake = FakeQuery(lead_init(), result_message(report()))

    result = await sdk_runner.investigate_alert(
        ALERT,
        journal=MemoryJournal(),
        environ=ENVIRON | {"AGENTPILOT_OPS_ARCHITECTURE": "swarm"},
        query=fake,
    )

    assert result.error_type is ErrorType.CONFIG and fake.options is None


# --- Options du SDK et consignes ------------------------------------------------------------


async def options(environ: dict[str, str]) -> Any:  # noqa: ANN401
    fake = FakeQuery(lead_init(), result_message(report()))
    await sdk_runner.investigate_alert(ALERT, journal=MemoryJournal(), environ=environ, query=fake)
    assert fake.options is not None
    return fake.options


async def test_solo_options_are_unchanged() -> None:
    found = await options(ENVIRON)

    assert found.tools == [] and found.disallowed_tools == BUILTIN_TOOLS
    assert found.agents is None
    assert sorted(found.allowed_tools) == sorted(OPS_TOOL_NAMES)
    assert "Enquêteurs délégués" not in found.system_prompt


async def test_subagents_options() -> None:
    found = await options(SUBAGENTS)

    assert found.tools == ["Agent"]
    assert "Agent" not in found.disallowed_tools
    assert {"Task", "Bash", "Read", "Write", "Edit", "Glob", "Grep"} <= set(found.disallowed_tools)
    assert sorted(found.allowed_tools) == sorted({*OPS_TOOL_NAMES, "Agent"})
    assert "Enquêteurs délégués" in found.system_prompt
    assert set(found.agents) == {LOGS, METRICS}
    for name, definition in found.agents.items():
        assert set(definition.tools) == SUBAGENT_TOOLS[name]
        assert not set(definition.tools) & NEVER_FOR_A_SUBAGENT
        assert definition.model == "inherit"
        assert definition.description and "## Réponse" in definition.prompt
        # Les subagents ne reçoivent pas le guide de diagnostic : ils ne concluent pas
        assert "Guide de diagnostic" not in definition.prompt


def test_lead_and_subagent_tools_split_the_five_ops_tools() -> None:
    subagent_tools = set().union(*SUBAGENT_TOOLS.values())
    assert (LEAD_TOOL_NAMES - {"Agent"}) | subagent_tools == OPS_TOOL_NAMES
    assert not (LEAD_TOOL_NAMES & subagent_tools)


def test_solo_fingerprint_is_unchanged_and_subagents_have_their_own() -> None:
    """La configuration de l'agent seul (`agents/ops/`) ne bouge pas : `reference-v2` reste
    comparable."""
    assert ops_agent.prompt_sha256()[:12] == "9704eed2dc32"
    assert ops_agent.prompt_sha256(Architecture.SUBAGENTS) != ops_agent.prompt_sha256()


@pytest.mark.parametrize("path", SUBAGENT_FILES, ids=lambda p: p.name)
def test_subagent_files_never_name_a_simulated_fault_or_a_forbidden_path(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for scenario in ChaosScenario:
        assert scenario.value not in text
    for word in ("chaos", "x-chaos-token", "tickets.jsonl", "incidents.jsonl", "5433", "evals/"):
        assert word not in text, word


def test_subagent_files() -> None:
    assert {p.stem for p in SUBAGENT_FILES} == {"CLAUDE", LOGS, METRICS}


# --- Enquête déléguée -------------------------------------------------------------------------


async def test_delegated_investigation_records_the_author_of_each_call() -> None:
    journal = MemoryJournal()

    result = await investigate(*delegated_investigation(), journal=journal)

    assert result.error is None, result.error
    assert result.output is not None and result.output["probable_cause"] == "database_errors"
    assert [(c.tool, c.agent, c.outcome.value) for c in journal.calls] == [
        ("Agent", None, "succeeded"),
        ("mcp__ops__summarize_logs", LOGS, "succeeded"),
        ("Agent", None, "succeeded"),
        ("mcp__ops__query_metrics", METRICS, "succeeded"),
    ]


async def test_lead_may_read_alerts_and_health_itself() -> None:
    journal = MemoryJournal()
    hooks = sdk_runner.ops_hooks(journal, Architecture.SUBAGENTS)

    for name in ("list_alerts", "check_health"):
        data = {"tool_name": f"mcp__ops__{name}", "tool_input": {}, "hook_event_name": "x"}
        assert await hooks.pre_tool_use(data, "t", {"signal": None}) == {}  # type: ignore[arg-type]
    assert journal.calls == []


# --- Refus : jamais d'autre agent, jamais un outil d'un autre ---------------------------------


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        # Agent généraliste (fichiers, shell) ou type inconnu : refusé
        (delegate("general-purpose"), "Type d'agent 'general-purpose' interdit"),
        (delegate("Explore"), "interdit"),
        (delegate(None), "Type d'agent None interdit"),
        (delegate("general-purpose", tool="Task"), "interdit"),
        # L'enquêteur principal ne lit ni logs ni métriques lui-même
        (ops_call("search_logs", service="shopsim"), "interdit"),
        (ops_call("query_metrics", metric="shopsim_error_rate"), "interdit"),
        (Call("Read", {"file_path": "/etc/passwd"}), "interdit"),
        (Call("Bash", {"command": "ls"}), "interdit"),
    ],
    ids=lambda x: x.name if isinstance(x, Call) else None,
)
async def test_lead_is_refused_and_it_is_logged(call: Call, reason: str) -> None:
    journal = MemoryJournal()

    await investigate(*delegated_investigation(), call, journal=journal)

    assert call.denied is not None and reason in call.denied
    [denied] = [c for c in journal.calls if c.outcome.value == "denied"]
    assert (denied.tool, denied.agent, denied.input) == (call.name, None, call.arguments)


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        # Un subagent n'a que ses outils : ni ceux de l'autre, ni ceux du principal
        (by(LOGS, "query_metrics", metric="shopsim_error_rate"), "interdit à logs-investigator"),
        (by(METRICS, "search_logs", service="shopsim"), "interdit à metrics-investigator"),
        (by(LOGS, "list_alerts"), "interdit"),
        # Ni fichiers, ni shell, ni délégation, ni sortie structurée
        (Call("Read", {"file_path": "evals/incidents.jsonl"}, agent=LOGS), "interdit"),
        (Call("Glob", {"pattern": "**/*.jsonl"}, agent=METRICS), "interdit"),
        (Call("Bash", {"command": "cat .env"}, agent=METRICS), "interdit"),
        (Call("Agent", {"subagent_type": LOGS}, agent=LOGS), "interdit"),
        (Call("StructuredOutput", {}, agent=LOGS), "interdit"),
        # Un agent généraliste qui aurait été lancé malgré tout n'a droit à rien
        (Call("Read", {"file_path": "x"}, agent="general-purpose"), "inconnu"),
        (by("general-purpose", "search_logs", service="shopsim"), "inconnu"),
    ],
    ids=lambda x: f"{x.agent}-{x.name}" if isinstance(x, Call) else None,
)
async def test_subagent_is_refused_and_it_is_logged(call: Call, reason: str) -> None:
    journal = MemoryJournal()

    await investigate(*delegated_investigation(), call, journal=journal)

    assert call.denied is not None and reason in call.denied
    [denied] = [c for c in journal.calls if c.outcome.value == "denied"]
    assert (denied.tool, denied.agent) == (call.name, call.agent)


async def test_refused_delegation_is_not_counted() -> None:
    hooks = sdk_runner.ops_hooks(MemoryJournal(), Architecture.SUBAGENTS)
    data = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose"},
        "hook_event_name": "PreToolUse",
    }

    await hooks.pre_tool_use(data, "t", {"signal": None})  # type: ignore[arg-type]

    assert hooks.delegated == {}


# --- Vérification d'isolement ----------------------------------------------------------------


def subagent_answer(name: str, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "name": name,
        "tools": sorted(SUBAGENT_TOOLS[name]),
        "can_access_files": False,
        "can_run_shell": False,
        "fault_token_header": None,
        "fault_log_file": None,
        "incidents_ground_truth_path": None,
        "tickets_ground_truth_path": None,
        "postgres_host_port": None,
    } | changes


def subagents_context(*answers: dict[str, Any]) -> dict[str, Any]:
    found = list(answers) or [subagent_answer(LOGS), subagent_answer(METRICS)]
    return context() | {"subagents": found}


async def check(
    answer: dict[str, Any], monkeypatch: pytest.MonkeyPatch, launched: tuple[str, ...] = ()
) -> sdk_runner.AgentResult:
    monkeypatch.setattr(ops_agent, "now", lambda: NOW)
    calls = [delegate(kind) for kind in (launched or (LOGS, METRICS))]
    fake = HookDrivingQuery(*calls, output=answer, executor=run_tool, init=lead_init())
    return await sdk_runner.check_ops_context(
        journal=MemoryJournal(), environ=SUBAGENTS, query=fake
    )


async def test_subagents_isolation_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await check(subagents_context(), monkeypatch)

    assert result.error is None, result.error
    assert result.output is not None and len(result.output["subagents"]) == 2


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        # Un subagent qui aurait des fichiers ou un shell
        (
            [subagent_answer(LOGS, tools=["Read", "search_logs"]), subagent_answer(METRICS)],
            "outils",
        ),
        ([subagent_answer(LOGS, tools=["Bash"]), subagent_answer(METRICS)], "outils"),
        ([subagent_answer(LOGS, can_access_files=True), subagent_answer(METRICS)], "shell"),
        ([subagent_answer(LOGS), subagent_answer(METRICS, can_run_shell=True)], "shell"),
        # Un subagent qui connaît la vérité terrain ou le jeton
        (
            [
                subagent_answer(LOGS, incidents_ground_truth_path="evals/incidents.jsonl"),
                subagent_answer(METRICS),
            ],
            "fuite (incidents_ground_truth_path)",
        ),
        (
            [subagent_answer(LOGS), subagent_answer(METRICS, fault_token_header="X-Chaos-Token")],
            "fuite (fault_token_header)",
        ),
        # Réponse manquante
        ([subagent_answer(LOGS)], "Réponses de subagents attendues"),
    ],
)
async def test_subagents_isolation_check_fails(
    answers: list[dict[str, Any]], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await check(subagents_context(*answers), monkeypatch)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.error is not None and message in result.error


async def test_subagents_isolation_check_requires_both_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Des réponses recopiées sans que le subagent ait été lancé ne prouvent rien."""
    result = await check(subagents_context(), monkeypatch, launched=(LOGS,))

    assert result.error is not None and "jamais lancés" in result.error
    assert METRICS in result.error


async def test_a_general_agent_launched_in_the_check_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await check(
        subagents_context(), monkeypatch, launched=(LOGS, METRICS, "general-purpose")
    )

    # Le lancement interdit est refusé ; les deux subagents prévus ont répondu
    assert result.error is None, result.error
