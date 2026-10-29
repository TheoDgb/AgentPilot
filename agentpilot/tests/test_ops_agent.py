"""Agent Ops (J6.2) avec un faux SDK : consigne, isolement, enquête de bout en bout sur les
vrais outils Ops (logs et Prometheus simulés), validation du rapport, hooks, vérification
d'isolement. Aucun appel réel, aucune base."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentpilot import ops_agent, sdk_runner
from agentpilot.hooks import OpsHooks
from agentpilot.ops_agent import (
    GUIDE_PATH,
    INSTRUCTIONS_PATH,
    OPS_TOOL_NAMES,
    SETTINGS_PATH,
    TOOLS,
    AlertBrief,
)
from agentpilot.ops_schemas import Confidence, IncidentCause
from agentpilot.sdk_runner import ErrorType
from agentpilot.support_agent import SUPPORT_TOOL_NAMES
from agentpilot.tests.conftest import (
    Call,
    FakeQuery,
    HookDrivingQuery,
    MemoryJournal,
    init_message,
    result_message,
)
from mcp_server.ops import common, sources
from mcp_server.ops.metrics_tools import promql
from observability import trace
from shopsim.app.models import ChaosScenario

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
ROUTE = "/orders/{order_id}"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
ALERT = AlertBrief(
    id=uuid.UUID("6b1f8d0e-1c2a-4f1a-9a55-0c6f0b0f8a11"),
    alertname="ShopSimHighErrorRate",
    route=ROUTE,
    status="firing",
    severity="critical",
    summary=f"Erreurs 5xx sur {ROUTE}",
    starts_at=NOW - timedelta(minutes=3),
    ends_at=None,
)
ENVIRON = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    # Ne doivent jamais atteindre l'agent Ops ni son serveur MCP
    "CHAOS_TOKEN": "chaos-secret",
    "SHOPSIM_MCP_ACTIONS": "1",
    "PROMETHEUS_URL": "http://prometheus.test:9090",
}
OPS_INIT_TOOLS = ["StructuredOutput", *sorted(OPS_TOOL_NAMES)]
AGENT_FILES = [INSTRUCTIONS_PATH, GUIDE_PATH, SETTINGS_PATH]


def ops_init(tools: list[str] | None = None, status: str = "connected") -> Any:  # noqa: ANN401
    return init_message(OPS_INIT_TOOLS if tools is None else tools, status, server="ops")


def ops_call(name: str, **arguments: Any) -> Call:  # noqa: ANN401
    return Call(f"mcp__ops__{name}", arguments)


def evidence(tool: str, field: str, value: Any, **arguments: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"tool": tool, "arguments": arguments, "field": field, "value": value}


def report(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    logs = evidence(
        "summarize_logs", f"requests.routes[{ROUTE}].5xx", 3, service="shopsim", since_minutes=5
    )
    metrics = evidence(
        "query_metrics", f"series[{ROUTE}].value", 1, metric="shopsim_error_rate", window="1m"
    )
    return {
        "alert_id": str(ALERT.id),
        "alertname": ALERT.alertname,
        "alert_route": ROUTE,
        "summary": "Les lectures des commandes échouent sur une erreur de base de données.",
        "symptoms": [
            {"description": "5xx sur les commandes", "routes": [ROUTE], "evidence": [logs]}
        ],
        "hypotheses": [
            {
                "cause": "database_errors",
                "status": "confirmed",
                "reasoning": "Exceptions de base de données et 100 % de 5xx.",
                "evidence": [logs, metrics],
            },
            {
                "cause": "resource_saturation",
                "status": "ruled_out",
                "reasoning": "Les durées restent courtes.",
                "evidence": [logs],
            },
        ],
        "probable_cause": "database_errors",
        "confidence": "high",
        "affected_routes": [ROUTE],
        "proposed_fix": "Vérifier l'état de la base de données et ses connexions.",
        "next_checks": [],
    } | changes


# --- Sources simulées pour les vrais outils Ops ------------------------------------------


@pytest.fixture
def ops_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Logs de ShopSim pendant une panne de base, Prometheus simulé, horloge figée."""
    lines = []
    for n in range(3):
        at = (NOW - timedelta(seconds=30 + n)).isoformat().replace("+00:00", "Z")
        base = {"ts": at, "service": "shopsim", "trace_id": f"{n:032x}", "route": ROUTE}
        lines.append(
            base | {"level": "error", "event": "http.request", "status": 500, "duration_ms": 8}
        )
        lines.append(
            base
            | {
                "level": "error",
                "event": "http.exception",
                "error_type": "psycopg.OperationalError",
            }
        )
    (tmp_path / "shopsim.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(common, "clock", lambda: NOW)
    error_rate = promql("shopsim_error_rate", "1m")["value"]

    def prometheus(request: httpx.Request) -> httpx.Response:
        found = []
        if request.url.params.get("query") == error_rate:
            found = [{"metric": {"route": ROUTE}, "value": [0, "1"]}]
        return httpx.Response(200, json={"status": "success", "data": {"result": found}})

    monkeypatch.setattr(sources, "transport", httpx.MockTransport(prometheus))
    return tmp_path


async def run_ops_tool(name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
    """Exécute le vrai outil Ops (logs et Prometheus simulés)."""
    tools = {tool.__name__: tool for tool in TOOLS}
    return await tools[name.rsplit("__", 1)[-1]](**arguments)


def investigation(*calls: Call, output: Any = None) -> HookDrivingQuery:  # noqa: ANN401
    return HookDrivingQuery(
        *calls, output=output or report(), executor=run_ops_tool, init=ops_init()
    )


def usual_calls() -> list[Call]:
    return [
        ops_call("summarize_logs", service="shopsim", since_minutes=5),
        ops_call("query_metrics", metric="shopsim_error_rate", window="1m"),
    ]


async def investigate(fake: Any, journal: MemoryJournal | None = None) -> sdk_runner.AgentResult:  # noqa: ANN401
    return await sdk_runner.investigate_alert(
        ALERT, journal=journal or MemoryJournal(), environ=ENVIRON, query=fake
    )


# --- Consigne et guide ---------------------------------------------------------------------


def test_agent_files_exist() -> None:
    for path in AGENT_FILES:
        assert path.is_file() and path.read_text(encoding="utf-8").strip(), path


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_agent_files_never_name_a_simulated_fault_or_a_forbidden_path(path: Path) -> None:
    """Le guide parle de familles de pannes, jamais des pannes simulées de ShopSim."""
    text = path.read_text(encoding="utf-8").lower()
    for scenario in ChaosScenario:
        assert scenario.value not in text
    for word in ("chaos", "x-chaos-token", "tickets.jsonl", "incidents.jsonl", "5433", "evals/"):
        assert word not in text, word


def test_prompt_states_the_pitfalls_causes_and_levels() -> None:
    prompt = ops_agent.build_system_prompt(NOW)

    # Pièges notés en J6.1
    assert "/health" in prompt and "panne de base de données" in prompt
    assert "survivre à un changement de cause" in prompt
    assert "estimés par tranches" in prompt
    # Liste fermée des causes et niveaux de confiance, tels que le schéma les attend
    for cause in IncidentCause:
        assert f"`{cause.value}`" in prompt
    for level in Confidence:
        assert f"`{level.value}`" in prompt
    for tool in TOOLS:
        assert f"`{tool.__name__}`" in prompt
    # Heure réelle du lancement, jamais la date de la simulation
    assert "Enquête lancée à 2026-10-29T18:00:00Z" in prompt
    assert "2026-07-06" not in prompt


def test_settings_allow_only_the_ops_tools() -> None:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))["permissions"]

    assert settings["allow"] == ["mcp__ops__*"]
    assert {"Bash", "Read", "Write", "WebFetch", "Agent", "Skill"} <= set(settings["deny"])


def test_ops_tools_are_the_five_read_tools() -> None:
    assert OPS_TOOL_NAMES == {
        "mcp__ops__search_logs",
        "mcp__ops__summarize_logs",
        "mcp__ops__query_metrics",
        "mcp__ops__list_alerts",
        "mcp__ops__check_health",
    }
    assert not OPS_TOOL_NAMES & SUPPORT_TOOL_NAMES


def test_alert_prompt_is_written_by_the_platform() -> None:
    prompt = ops_agent.alert_prompt(ALERT, NOW)

    assert f"- identifiant : {ALERT.id}" in prompt
    assert f"- route : {ROUTE}" in prompt
    assert "- début : il y a 3 min 00 s" in prompt
    assert "résolue" not in prompt


# --- Options : isolement -------------------------------------------------------------------


async def test_options_isolate_the_ops_agent(
    ops_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_agent, "now", lambda: NOW)
    fake = investigation(*usual_calls())

    with trace.bind(trace_id=TRACE):
        result = await investigate(fake)

    assert result.error is None, result.error
    options = fake.options
    assert options is not None
    assert options.setting_sources == [] and options.skills == [] and options.plugins == []
    assert options.strict_mcp_config is True
    assert options.system_prompt == ops_agent.build_system_prompt(NOW)
    # Les 5 outils Ops seulement : aucun outil intégré, aucun outil de l'agent Support
    assert options.tools == []
    assert options.allowed_tools == sorted(OPS_TOOL_NAMES)
    assert {"Bash", "Read", "WebFetch", "Agent", "Skill"} <= set(options.disallowed_tools)
    assert options.permission_mode == "dontAsk"
    # Plafonds d'une enquête
    assert (options.max_budget_usd, options.max_turns) == (1.0, 25)
    assert ops_agent.TIMEOUT_S == 600
    # Serveur MCP Ops, environnement construit champ par champ
    assert set(options.mcp_servers) == {"ops"}  # type: ignore[arg-type]
    server = options.mcp_servers["ops"]  # type: ignore[index]
    assert server["args"] == ["-m", "mcp_server.ops.server"]
    env = server["env"]
    assert env["PROMETHEUS_URL"] == "http://prometheus.test:9090"
    assert env["AGENTPILOT_URL"] == "http://localhost:8001"
    assert Path(env["LOG_DIR"]).is_absolute()
    # La trace de l'exécution suit jusqu'au serveur MCP
    assert trace.parse_traceparent(env[trace.TRACEPARENT_ENV]) == TRACE
    # Jamais le jeton des pannes, ni rien d'autre de l'environnement du service
    everything = json.dumps(server) + json.dumps(options.env) + (options.system_prompt or "")
    assert "chaos-secret" not in everything and "CHAOS_TOKEN" not in everything
    assert "SHOPSIM_MCP_ACTIONS" not in env
    assert "CHAOS_TOKEN" not in options.env
    # Dossiers de travail temporaires (vides au lancement)
    assert options.cwd and Path(options.cwd).name == "cwd"


async def test_chaos_token_in_env_file_never_reaches_the_agent(
    ops_sources: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jeton dans le `.env` et dans l'environnement : ni le serveur MCP ni la CLI ne le voient."""
    from agentpilot import env_file, support_agent

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-file\nCHAOS_TOKEN=from-env-file\n")
    monkeypatch.setattr(env_file, "ENV_FILE", env)
    monkeypatch.setattr(support_agent, "ENV_FILE", env)
    fake = investigation(*usual_calls())

    await sdk_runner.investigate_alert(
        ALERT,
        journal=MemoryJournal(),
        environ={"CHAOS_TOKEN": "from-shell"},
        query=fake,
    )

    assert fake.options is not None
    seen = json.dumps(fake.options.mcp_servers) + json.dumps(fake.options.env)
    assert "from-env-file" not in seen and "from-shell" not in seen


# --- Initialisation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("init", "message"),
    [
        (ops_init([*OPS_INIT_TOOLS, "mcp__shopsim__get_order"]), "outils interdits"),
        (ops_init([*OPS_INIT_TOOLS, "Bash"]), "outils interdits"),
        (ops_init(status="failed"), "Serveur MCP ops non connecté"),
        (ops_init(OPS_INIT_TOOLS[:-1]), "Outils ops manquants"),
    ],
)
async def test_wrong_tools_at_initialisation_are_refused(init: Any, message: str) -> None:  # noqa: ANN401
    fake = FakeQuery(init, result_message(report()))

    result = await investigate(fake)

    assert result.error is not None and message in result.error
    assert result.output is None


# --- Enquête de bout en bout -------------------------------------------------------------


async def test_investigation_with_the_real_ops_tools(ops_sources: Path) -> None:
    journal = MemoryJournal()
    calls = usual_calls()
    fake = investigation(*calls)

    result = await investigate(fake, journal)

    assert result.error is None, result.error
    assert result.output is not None
    assert result.output["probable_cause"] == "database_errors"
    assert result.simulation_date is None
    # Les outils ont lu les sources simulées
    summary = calls[0].response
    assert summary["requests"]["routes"][0]["5xx"] == 3
    assert calls[1].response["series"] == [{"labels": {"route": ROUTE}, "value": 1}]
    # Journal des appels, comme pour l'agent Support
    assert [(c.tool, c.outcome.value) for c in journal.calls] == [
        ("mcp__ops__summarize_logs", "succeeded"),
        ("mcp__ops__query_metrics", "succeeded"),
    ]
    assert journal.calls[0].response["service"] == "shopsim"


async def test_evidence_from_a_tool_never_called_is_refused(ops_sources: Path) -> None:
    """Le rapport cite `query_metrics`, que l'agent n'a jamais appelé : preuve inventée."""
    fake = investigation(ops_call("summarize_logs", service="shopsim", since_minutes=5))

    result = await investigate(fake)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.error is not None and "jamais appelés" in result.error
    assert "query_metrics" in result.error
    assert result.output is None


@pytest.mark.parametrize(
    "changes",
    [
        {"alert_id": str(uuid.uuid4())},
        {"alertname": "ShopSimHighLatency"},
        {"alert_route": "/payments/{order_id}"},
    ],
)
async def test_report_on_another_alert_is_refused(
    ops_sources: Path, changes: dict[str, Any]
) -> None:
    fake = investigation(*usual_calls(), output=report(**changes))

    result = await investigate(fake)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.output is None


async def test_inconsistent_report_is_refused(ops_sources: Path) -> None:
    fake = investigation(*usual_calls(), output=report(probable_cause="slow_dependency"))

    result = await investigate(fake)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.error is not None and "Rapport invalide" in result.error


# --- Hooks -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["mcp__shopsim__refund_order", "mcp__shopsim__get_order", "Bash", "Read"]
)
async def test_any_other_tool_is_denied_and_logged(ops_sources: Path, tool: str) -> None:
    journal = MemoryJournal()
    other = Call(tool, {"order_id": 1})
    fake = investigation(*usual_calls(), other)

    await investigate(fake, journal)

    assert other.denied is not None and "interdit" in other.denied
    denied = [c for c in journal.calls if c.tool == tool]
    assert [c.outcome.value for c in denied] == ["denied"]


async def test_ops_hooks_let_only_ops_tools_through() -> None:
    hooks = OpsHooks(MemoryJournal(), OPS_TOOL_NAMES)

    for name in [*OPS_TOOL_NAMES, "StructuredOutput"]:
        data = {"tool_name": name, "tool_input": {}, "hook_event_name": "PreToolUse"}
        assert await hooks.pre_tool_use(data, "t", {"signal": None}) == {}  # type: ignore[arg-type]


# --- Vérification d'isolement ------------------------------------------------------------


def context(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "causes": [c.value for c in IncidentCause],
        "confidence_levels": [c.value for c in Confidence],
        "tools": [tool.__name__ for tool in TOOLS],
        "launched_at": "2026-10-29T18:00:00Z",
        "fault_token_header": None,
        "fault_log_file": None,
        "incidents_ground_truth_path": None,
        "tickets_ground_truth_path": None,
        "postgres_host_port": None,
    } | changes


async def check(answer: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> sdk_runner.AgentResult:
    monkeypatch.setattr(ops_agent, "now", lambda: NOW)
    fake = FakeQuery(ops_init(), result_message(answer))
    result = await sdk_runner.check_ops_context(
        journal=MemoryJournal(), environ=ENVIRON, query=fake
    )
    assert fake.prompt == ops_agent.CHECK_PROMPT
    return result


async def test_isolation_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await check(context(), monkeypatch)

    assert result.error is None, result.error
    assert result.output is not None and result.output["fault_log_file"] is None


@pytest.mark.parametrize(
    ("trap", "value"),
    [
        ("fault_token_header", "X-Chaos-Token"),
        ("fault_log_file", "chaos.jsonl"),
        ("incidents_ground_truth_path", "evals/incidents.jsonl"),
        ("tickets_ground_truth_path", "evals/tickets.jsonl"),
        ("postgres_host_port", "5433"),
    ],
)
async def test_isolation_check_fails_on_a_leak(
    trap: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await check(context(**{trap: value}), monkeypatch)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.error is not None and "fuite" in result.error and trap in result.error


@pytest.mark.parametrize(
    "changes",
    [
        {"causes": ["database_errors"]},
        {"confidence_levels": ["élevé", "moyen", "faible"]},
        {"tools": ["search_logs"]},
        {"launched_at": "2026-07-06T12:00:00Z"},
    ],
)
async def test_isolation_check_fails_when_the_prompt_is_incomplete(
    changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await check(context(**changes), monkeypatch)

    assert result.error_type is ErrorType.INVALID_OUTPUT
    assert result.error is not None and "n'arrive pas en entier" in result.error


def test_developer_only_facts_are_in_the_developer_claude_md() -> None:
    """Les réponses pièges existent bien dans le CLAUDE.md développeur : une fuite serait
    détectée."""
    developer = (ops_agent.REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for fact in ("X-Chaos-Token", "chaos.jsonl", "incidents.jsonl", "evals/tickets.jsonl", "5433"):
        assert fact in developer
    assert re.search(r"Agent Ops", developer)
