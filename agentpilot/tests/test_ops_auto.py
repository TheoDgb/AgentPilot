"""Enquête automatique de l'agent Ops sur les alertes et rapport sans IA (J6.5) : tri des
alertes (regroupement, exclusions, délai), lancement dans les places et le budget, mode
dégradé, prise en compte par un humain, pages. Faux SDK, outils Ops réels sur des sources
simulées, vraie base de test."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import control, ops_agent, ops_auto, ops_degraded
from agentpilot.app import app
from agentpilot.journal import CallEntry
from agentpilot.models import (
    AgentKind,
    Alert,
    AlertStatus,
    PauseReason,
    Run,
    RunStatus,
    RunTrigger,
    Triage,
)
from agentpilot.ops_agent import AlertBrief
from agentpilot.ops_auto import AlertRow, plan
from agentpilot.ops_schemas import DegradedReason, DegradedReport
from agentpilot.tests.conftest import FakeQuery, MemoryJournal, result_message
from agentpilot.tests.test_ops_agent import ops_init, report
from agentpilot.worker import MAX_CONCURRENT_RUNS, Worker

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
LATENCY, ERRORS = "ShopSimHighLatency", "ShopSimHighErrorRate"
ROUTES = [
    "/customers/{customer_id}",
    "/customers/{customer_id}/orders",
    "/orders/{order_id}",
    "/payments/{order_id}",
    "/shipments/{order_id}",
]
API_LIMIT = "API Error: 400 You have reached your specified API usage limits."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def row(
    name: str = LATENCY,
    started_s: float = 0,
    received_s: float = -120,
    status: AlertStatus = AlertStatus.FIRING,
) -> AlertRow:
    """Alerte née `started_s` secondes après NOW (négatif : avant), reçue à `received_s`."""
    return AlertRow(
        uuid.uuid4(),
        name,
        status,
        NOW + timedelta(seconds=started_s),
        NOW + timedelta(seconds=received_s),
    )


# --- Tri, sans base ---------------------------------------------------------------------------


def test_saturation_five_latency_alerts_make_one_investigation() -> None:
    alerts = [row(started_s=-200 + 7 * i) for i in range(5)]

    found = plan(alerts, [], NOW)

    [group] = found.groups
    assert group.head == alerts[0] and group.covered == alerts[1:]


def test_two_alert_names_make_two_investigations() -> None:
    """Base de données en erreur et paiements lents : deux pannes de natures différentes."""
    found = plan([row(ERRORS, -200), row(ERRORS, -190), row(LATENCY, -150)], [], NOW)

    assert [(g.head.alertname, len(g.covered)) for g in found.groups] == [
        (ERRORS, 1),
        (LATENCY, 0),
    ]


def test_same_name_six_minutes_apart_makes_two_investigations() -> None:
    first, later = row(started_s=-600), row(started_s=-240)

    assert [g.head for g in plan([first, later], [], NOW).groups] == [first, later]


def test_alert_of_an_investigated_group_is_covered() -> None:
    head, late = row(started_s=-300), row(started_s=-100)

    found = plan([late], [head], NOW)

    assert found.covered == [(late, head.id)] and found.groups == []


def test_resolved_alert_is_skipped() -> None:
    found = plan([row(status=AlertStatus.RESOLVED)], [], NOW)

    assert len(found.skipped) == 1 and found.groups == []


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("AgentPilotDown", "plateforme injoignable"),
        ("AgentPilotStateUnavailable", "base illisible"),
        ("AgentBudgetNearlyReached", "aggraverait"),
        ("AgentRunsFailing", "l'IA échoue déjà"),
        ("PendingActionTooOld", "décision humaine"),
        ("OpsReportWithoutAI", "boucle"),
        ("SomeNewRule", "pas encore autorisée"),
    ],
)
def test_only_shop_symptoms_are_investigated(name: str, reason: str) -> None:
    found = plan([row(name)], [], NOW)

    [(alert, why)] = found.excluded
    assert alert.alertname == name and reason in why and found.groups == []


def test_alert_waits_for_its_neighbours() -> None:
    """Reçue il y a 30 s : elle attend la fin du délai de regroupement, et retient la voisine
    déjà prête (qui ne devient pas la tête du groupe à sa place)."""
    fresh = row(started_s=-60, received_s=-30)
    neighbour = row(started_s=-55, received_s=-90)

    found = plan([fresh, neighbour], [], NOW)

    assert found.groups == []
    assert found.next_ready == NOW + timedelta(seconds=30)


def test_shop_down_is_investigated() -> None:
    assert len(plan([row("ShopSimDown")], [], NOW).groups) == 1


# --- Rapport sans IA ---------------------------------------------------------------------------

BRIEF = AlertBrief(
    id=uuid.uuid4(),
    alertname=ERRORS,
    route="/orders/{order_id}",
    status="firing",
    severity="critical",
    summary="Erreurs 5xx",
    starts_at=NOW - timedelta(minutes=3),
    ends_at=None,
)
REPLIES: dict[str, Any] = {
    "check_health": {
        "services": [
            {"service": "ShopSim", "status": "ok"},
            {"service": "Prometheus", "status": "down", "reason": "injoignable"},
        ]
    },
    "list_alerts": {
        "alerts": [{"alertname": ERRORS, "route": "/orders/{order_id}", "started": "il y a 3 min"}]
    },
    "summarize_logs": {
        "requests": {
            "routes": [
                {"route": "/orders/{order_id}", "count": 60, "5xx": 60, "p95_ms": 9},
                {"route": "/customers/{customer_id}/orders", "count": 40, "5xx": 6, "p95_ms": 11},
                {"route": "/payments/{order_id}", "count": 50, "5xx": 2, "p95_ms": 4011},
                {"route": "/health", "count": 9, "5xx": 9, "p95_ms": 1},
            ]
        },
        "warnings": [
            {"event": "http.exception", "error_type": "psycopg.OperationalError", "count": 66},
            {"event": "http.request", "error_type": None, "count": 70},
        ],
    },
    "shopsim_error_rate": {"series": [{"labels": {"route": "/orders/{order_id}"}, "value": 1.0}]},
    "shopsim_latency_trend": {
        "series": [{"route": "/payments/{order_id}", "p95_s": {"now": 4.875, "15m_ago": 0.24}}]
    },
}


def fake_tools(fail: set[str] | None = None) -> ops_degraded.Connect:
    @asynccontextmanager
    async def connect() -> AsyncIterator[ops_degraded.Caller]:
        async def call(name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
            key = arguments.get("metric", name)
            if key in (fail or set()):
                raise ops_degraded.ToolFailed(f"{key} injoignable")
            return REPLIES[key]

        yield call

    return connect


async def degraded(
    journal: MemoryJournal | None = None, connect: ops_degraded.Connect | None = None
) -> DegradedReport:
    return await ops_degraded.degraded_report(
        BRIEF,
        DegradedReason.BUDGET,
        journal=journal or MemoryJournal(),
        connect=connect or fake_tools(),
        now=lambda: NOW,
    )


async def test_degraded_report_gives_facts_only() -> None:
    journal = MemoryJournal()

    found = await degraded(journal)

    assert found.without_ai is True and "aucune cause" in found.notice
    assert [(r.route, r.errors_5xx, r.error_rate_5m) for r in found.error_routes] == [
        ("/orders/{order_id}", 60, 1.0),
        ("/customers/{customer_id}/orders", 6, None),
    ]
    assert [(e.error_type, e.count) for e in found.error_types] == [
        ("psycopg.OperationalError", 66)
    ]
    [slow] = found.slow_routes
    assert (slow.route, slow.p95_ms, slow.p95_now_s, slow.p95_15m_ago_s) == (
        "/payments/{order_id}",
        4011,
        4.875,
        0.24,
    )
    assert found.affected_routes == [
        "/customers/{customer_id}/orders",
        "/orders/{order_id}",
        "/payments/{order_id}",
    ]
    assert found.observed_routes == 3  # /health ne compte pas
    assert [(h.service, h.status) for h in found.health] == [
        ("ShopSim", "ok"),
        ("Prometheus", "down"),
    ]
    assert found.active_alerts[0].started == "il y a 3 min"
    # Jamais de cause, d'hypothèse ni de confiance
    dumped = json.dumps(found.model_dump())
    for word in ("probable_cause", "hypotheses", "confidence"):
        assert word not in dumped
    # Chaque appel au journal, auteur « sans IA »
    assert [(c.tool.rsplit("__", 1)[-1], c.agent) for c in journal.calls] == [
        ("check_health", "no_ai"),
        ("list_alerts", "no_ai"),
        ("summarize_logs", "no_ai"),
        ("query_metrics", "no_ai"),
        ("query_metrics", "no_ai"),
    ]
    assert all(isinstance(c, CallEntry) and c.outcome.value == "succeeded" for c in journal.calls)


async def test_unavailable_source_is_reported_not_fatal() -> None:
    journal = MemoryJournal()

    found = await degraded(journal, fake_tools({"shopsim_latency_trend", "list_alerts"}))

    assert found.unavailable == [
        "list_alerts : list_alerts injoignable",
        "query_metrics : shopsim_latency_trend injoignable",
    ]
    assert found.active_alerts == [] and found.error_routes  # le reste est là
    assert [c.outcome.value for c in journal.calls].count("failed") == 2


async def test_mcp_server_that_does_not_start_still_gives_a_report() -> None:
    @asynccontextmanager
    async def broken() -> AsyncIterator[ops_degraded.Caller]:
        raise RuntimeError("serveur absent")
        yield  # pragma: no cover

    found = await degraded(connect=broken)

    assert found.unavailable and "serveur MCP Ops" in found.unavailable[0]
    assert found.error_routes == [] and found.health == []


async def test_degraded_tools_get_the_agent_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Même configuration que l'agent : environnement construit champ par champ, jamais
    `CHAOS_TOKEN` (le serveur n'est pas lancé ici)."""
    seen: dict[str, Any] = {}
    original = ops_agent.mcp_server_config

    def config(environ: Any, trace_id: Any, log_dir: Any) -> dict[str, Any]:  # noqa: ANN401
        seen.update(original(environ, trace_id=trace_id, log_dir=log_dir))
        raise RuntimeError("arrêt du test")

    monkeypatch.setattr(ops_agent, "mcp_server_config", config)

    found = await ops_degraded.degraded_report(
        BRIEF,
        DegradedReason.AI_UNAVAILABLE,
        journal=MemoryJournal(),
        connect=lambda: ops_degraded.mcp_tools({"CHAOS_TOKEN": "secret"}),
    )

    assert seen["args"] == ["-m", "mcp_server.ops.server"]
    assert "CHAOS_TOKEN" not in seen["env"] and "secret" not in json.dumps(seen)
    assert found.unavailable


async def test_degraded_report_through_the_real_mcp_server(tmp_path: Path) -> None:
    """Vrai serveur MCP Ops (stdio) sur des logs simulés ; Prometheus et AgentPilot
    injoignables : ils figurent comme tels, le reste du rapport est là."""
    at = (datetime.now(UTC) - timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    lines = [
        {
            "ts": at,
            "service": "shopsim",
            "level": "error",
            "event": "http.request",
            "route": "/orders/{order_id}",
            "status": 500,
            "duration_ms": 8,
        }
        for _ in range(5)
    ]
    (tmp_path / "shopsim.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    environ = {
        "PROMETHEUS_URL": "http://127.0.0.1:9",
        "AGENTPILOT_URL": "http://127.0.0.1:9",
        "SHOPSIM_URL": "http://127.0.0.1:9",
        "ALERTMANAGER_URL": "http://127.0.0.1:9",
    }
    journal = MemoryJournal()

    @asynccontextmanager
    async def connect() -> AsyncIterator[ops_degraded.Caller]:
        original = ops_agent.logs_to_read
        ops_agent.logs_to_read = lambda configured: tmp_path  # type: ignore[assignment]
        try:
            async with ops_degraded.mcp_tools(environ) as call:
                yield call
        finally:
            ops_agent.logs_to_read = original  # type: ignore[assignment]

    found = await ops_degraded.degraded_report(
        BRIEF, DegradedReason.BUDGET, journal=journal, connect=connect
    )

    assert [(r.route, r.errors_5xx) for r in found.error_routes] == [("/orders/{order_id}", 5)]
    assert {h.status for h in found.health} == {"down"}
    assert any(u.startswith("query_metrics") for u in found.unavailable)
    assert len(journal.calls) == 5


# --- Lancement par la boucle (base de test) ---------------------------------------------------


def add_alert(
    factory: sessionmaker[Session],
    name: str = LATENCY,
    route: str = "/orders/{order_id}",
    started_s: float = -200,
    received_s: float = -120,
    status: AlertStatus = AlertStatus.FIRING,
) -> uuid.UUID:
    now = datetime.now(UTC)
    alert = Alert(
        fingerprint=uuid.uuid4().hex[:16],
        alertname=name,
        severity="warning",
        route=route,
        status=status,
        summary=f"{name} sur {route}",
        starts_at=now + timedelta(seconds=started_s),
        ends_at=None,
        first_received_at=now + timedelta(seconds=received_s),
        last_received_at=now,
    )
    with factory() as session:
        session.add(alert)
        session.commit()
        return alert.id


def switch(factory: sessionmaker[Session], on: bool = True) -> None:
    control.set_ops_auto(factory, on, "Astreinte")


class GatedQuery(FakeQuery):
    """Faux SDK d'enquête qui attend un signal (places occupées) et compte ses lancements."""

    def __init__(self, *messages: Any) -> None:  # noqa: ANN401
        super().__init__(*messages)
        self.gate = asyncio.Event()
        self.gate.set()
        self.launches = 0

    async def _stream(self) -> AsyncIterator[Any]:
        self.launches += 1
        await self.gate.wait()
        async for message in super()._stream():
            yield message


def make_worker(
    factory: sessionmaker[Session],
    query: Any = None,  # noqa: ANN401
) -> Worker:
    state = SimpleNamespace(
        session_factory=factory,
        query=query or GatedQuery(ops_init(), result_message(report())),
        ops_tools=fake_tools(),
    )
    return Worker(state)


def alerts_of(factory: sessionmaker[Session]) -> dict[str, Alert]:
    with factory() as session:
        return {a.route or a.alertname: a for a in session.scalars(select(Alert))}


def runs_of(factory: sessionmaker[Session]) -> list[Run]:
    with factory() as session:
        return list(session.scalars(select(Run).order_by(Run.created_at)))


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv(control.BUDGET_ENV, "100")


async def test_switch_off_launches_nothing(session_factory: sessionmaker[Session]) -> None:
    add_alert(session_factory)
    work = make_worker(session_factory)

    assert await work.triage_alerts() is None

    assert runs_of(session_factory) == []
    assert all(a.triage is None for a in alerts_of(session_factory).values())


@pytest.mark.usefixtures("api_key")
async def test_saturation_is_investigated_once(session_factory: sessionmaker[Session]) -> None:
    for i, route in enumerate(ROUTES):
        add_alert(session_factory, route=route, started_s=-200 + 5 * i)
    switch(session_factory)
    work = make_worker(session_factory)

    await work.triage_alerts()
    await work.drain()
    # Un second tour, des webhooks rejoués : rien de plus
    await work.triage_alerts()
    await work.drain()

    [run] = runs_of(session_factory)
    assert (run.agent, run.trigger) == (AgentKind.OPS, RunTrigger.ALERT)
    found = alerts_of(session_factory)
    head = found[ROUTES[0]]
    assert head.triage is Triage.INVESTIGATED and head.triage_run_id == run.id
    assert run.alert_id == head.id
    for route in ROUTES[1:]:
        assert (found[route].triage, found[route].covered_by) == (Triage.COVERED, head.id)


@pytest.mark.usefixtures("api_key")
async def test_alert_received_just_now_waits(session_factory: sessionmaker[Session]) -> None:
    add_alert(session_factory, received_s=-10)
    switch(session_factory)
    work = make_worker(session_factory)

    delay = await work.triage_alerts()

    assert delay is not None and 40 <= delay <= 50
    assert runs_of(session_factory) == []


@pytest.mark.usefixtures("api_key")
async def test_excluded_and_resolved_alerts_are_recorded_with_their_reason(
    session_factory: sessionmaker[Session],
) -> None:
    add_alert(session_factory, "AgentBudgetNearlyReached", route="")
    add_alert(session_factory, route="/shipments/{order_id}", status=AlertStatus.RESOLVED)
    switch(session_factory)
    work = make_worker(session_factory)

    await work.triage_alerts()

    found = alerts_of(session_factory)
    assert found["AgentBudgetNearlyReached"].triage is Triage.EXCLUDED
    assert "aggraverait" in (found["AgentBudgetNearlyReached"].triage_detail or "")
    assert found["/shipments/{order_id}"].triage is Triage.SKIPPED
    assert runs_of(session_factory) == []


@pytest.mark.usefixtures("api_key")
async def test_alerts_wait_for_a_free_place(session_factory: sessionmaker[Session]) -> None:
    """Places occupées : les alertes de tête attendent, sans être triées ; puis passent."""
    query = GatedQuery(ops_init(), result_message(report()))
    query.gate.clear()
    for name in (ERRORS, LATENCY, "ShopSimDown"):
        add_alert(session_factory, name, route=name)
    switch(session_factory)
    work = make_worker(session_factory, query)

    await work.triage_alerts()
    await asyncio.sleep(0.05)

    assert len(work.active) == MAX_CONCURRENT_RUNS
    waiting = [a for a in alerts_of(session_factory).values() if a.triage is None]
    assert len(waiting) == 1
    query.gate.set()
    await work.drain()
    await work.triage_alerts()
    await work.drain()
    assert len([r for r in runs_of(session_factory) if r.agent is AgentKind.OPS]) == 3


@pytest.mark.usefixtures("api_key")
async def test_budget_gives_a_degraded_report_without_pausing_the_loop(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(control.BUDGET_ENV, "0.5")
    add_alert(session_factory, ERRORS)
    switch(session_factory)
    work = make_worker(session_factory)

    await work.triage_alerts()
    await work.drain()

    [run] = runs_of(session_factory)
    assert (run.agent, run.status, run.trigger) == (
        AgentKind.OPS_DEGRADED,
        RunStatus.SUCCEEDED,
        RunTrigger.ALERT,
    )
    assert run.total_cost_usd == Decimal(0) and run.output is not None
    assert run.output["reason"] == "budget" and run.output["without_ai"] is True
    assert [c.agent for c in run.tool_calls] == ["no_ai"] * 5
    [alert] = alerts_of(session_factory).values()
    assert (alert.triage, alert.triage_run_id) == (Triage.DEGRADED, run.id)
    # La boucle Support n'est pas mise en pause pour autant
    assert control.load(session_factory).pause_reason is None


@pytest.mark.usefixtures("api_key")
async def test_known_ai_outage_gives_a_degraded_report(
    session_factory: sessionmaker[Session],
) -> None:
    control.pause(session_factory, None, PauseReason.INFRA, "api_unavailable")
    add_alert(session_factory, ERRORS)
    switch(session_factory)
    work = make_worker(session_factory)

    await work.triage_alerts()
    await work.drain()

    [run] = runs_of(session_factory)
    assert run.agent is AgentKind.OPS_DEGRADED and run.output["reason"] == "ai_unavailable"  # type: ignore[index]


@pytest.mark.usefixtures("api_key")
async def test_hourly_cap_gives_a_degraded_report(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ops_auto.MAX_PER_HOUR_ENV, "1")
    add_alert(session_factory, ERRORS, started_s=-900)
    add_alert(session_factory, ERRORS, route="/payments/{order_id}", started_s=-200)
    switch(session_factory)
    work = make_worker(session_factory)

    await work.triage_alerts()
    await work.drain()

    kinds = [(r.agent, (r.output or {}).get("reason")) for r in runs_of(session_factory)]
    assert sorted(kinds, key=str) == sorted(
        [(AgentKind.OPS, None), (AgentKind.OPS_DEGRADED, "hourly_cap")], key=str
    )


@pytest.mark.usefixtures("api_key")
async def test_infrastructure_error_of_the_investigation_gives_a_degraded_report(
    session_factory: sessionmaker[Session],
) -> None:
    query = GatedQuery(ops_init(), result_message(None, is_error=True, result=API_LIMIT))
    add_alert(session_factory, ERRORS)
    switch(session_factory)
    work = make_worker(session_factory, query)

    await work.triage_alerts()
    await work.drain()

    ai, fallback = runs_of(session_factory)
    assert (ai.agent, ai.status, ai.error_type) == (
        AgentKind.OPS,
        RunStatus.FAILED,
        "api_unavailable",
    )
    assert fallback.agent is AgentKind.OPS_DEGRADED and fallback.alert_id == ai.alert_id
    assert fallback.output["reason"] == "infra_error"  # type: ignore[index]
    assert fallback.output["reason_detail"] == "api_unavailable"  # type: ignore[index]
    [alert] = alerts_of(session_factory).values()
    assert (alert.triage, alert.triage_run_id) == (Triage.DEGRADED, fallback.id)
    # La boucle Support se met en pause, comme pour toute erreur d'API (J5)
    assert control.load(session_factory).pause_reason is PauseReason.INFRA


@pytest.mark.usefixtures("api_key")
async def test_agent_fault_gives_no_degraded_report(
    session_factory: sessionmaker[Session],
) -> None:
    """Rapport invalide : faute de l'agent, l'exécution en échec reste visible, sans relève."""
    query = GatedQuery(ops_init(), result_message({"invalide": True}))
    add_alert(session_factory, ERRORS)
    switch(session_factory)
    work = make_worker(session_factory, query)

    await work.triage_alerts()
    await work.drain()

    [run] = runs_of(session_factory)
    assert (run.agent, run.error_type) == (AgentKind.OPS, "invalid_output")


# --- Pages et prise en compte -----------------------------------------------------------------


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> TestClient:
    app.state.session_factory = session_factory
    return TestClient(app)


@pytest.mark.usefixtures("api_key")
async def test_pages_and_review(
    client: TestClient, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(control.BUDGET_ENV, "0.5")
    add_alert(session_factory, ERRORS, route="/orders/{order_id}")
    add_alert(session_factory, ERRORS, route="/customers/{customer_id}/orders", started_s=-190)
    add_alert(session_factory, "PendingActionTooOld", route="<script>")
    switch(session_factory)
    work = make_worker(session_factory)
    await work.triage_alerts()
    await work.drain()
    [run] = runs_of(session_factory)

    listing = client.get("/investigations").text
    assert "sans IA" in listing and "à lire" in listing
    assert "/customers/{customer_id}/orders" in listing  # alerte couverte
    assert "décision humaine en attente" in listing and "&lt;script&gt;" in listing
    assert "<script>" not in listing
    detail = client.get(f"/investigations/{run.id}").text
    assert "Rapport sans IA" in detail and "psycopg.OperationalError" in detail
    assert "aucune cause" in detail
    control_page = client.get("/control").text
    assert "1 rapport sans IA à lire" in control_page and "Active" in control_page
    assert client.get("/control/status").json()["degraded_unreviewed"] == 1
    assert "agentpilot_ops_degraded_unreviewed 1.0" in client.get("/metrics").text

    refused = client.post(f"/investigations/{run.id}/review", data={"name": ""})
    assert refused.status_code == 422
    done = client.post(f"/investigations/{run.id}/review", data={"name": "Astreinte"})
    assert done.status_code == 200 and "pris en compte" in done.text
    assert client.get("/control/status").json()["degraded_unreviewed"] == 0
    again = client.post(f"/investigations/{run.id}/review", data={"name": "Astreinte"})
    assert "déjà pris en compte" in again.text


def test_ops_auto_switch_requires_a_name(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert client.post("/control/ops-auto", data={"on": "true", "name": ""}).status_code == 422
    assert control.load(session_factory).ops_auto is False
    client.post("/control/ops-auto", data={"on": "true", "name": "Astreinte"})
    status = client.get("/control/status").json()
    assert status["ops_auto"] is True and status["ops_auto_by"] == "Astreinte"
    assert "agentpilot_ops_auto 1.0" in client.get("/metrics").text


def test_investigation_of_an_ai_report_is_rendered(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    alert_id = add_alert(session_factory, ERRORS)
    with session_factory() as session:
        run = Run(
            agent=AgentKind.OPS,
            alert_id=alert_id,
            status=RunStatus.SUCCEEDED,
            trigger=RunTrigger.ALERT,
            created_at=datetime.now(UTC),
            output=report(alert_id=str(alert_id)),
            total_cost_usd=Decimal("0.06"),
        )
        session.add(run)
        session.commit()
        run_id = run.id

    page = client.get(f"/investigations/{run_id}").text

    assert "database_errors" in page and "Correctif proposé" in page
    assert "écartée" in page and "requests.routes[/orders/{order_id}].5xx" in page
    assert client.get(f"/investigations/{uuid.uuid4()}").status_code == 404


def test_reset_turns_the_switch_off(
    session_factory: sessionmaker[Session], agentpilot_engine: Engine
) -> None:
    from agentpilot.db import clear

    switch(session_factory)
    clear(agentpilot_engine)

    assert control.load(session_factory).ops_auto is False


def test_init_db_upgrades_an_existing_database_for_j6_5(upgraded_engine: Engine) -> None:
    """Base d'avant J6.5 : colonnes, type et valeurs ajoutés ; `init_db` deux fois."""
    from sqlalchemy import text

    from agentpilot.db import SCHEMA, init_db

    with upgraded_engine.begin() as connection:
        for table, column in (
            ("control", "ops_auto"),
            ("alerts", "triage"),
            ("alerts", "covered_by"),
            ("runs", "reviewed_at"),
        ):
            connection.execute(text(f"ALTER TABLE {SCHEMA}.{table} DROP COLUMN {column}"))
        connection.execute(text(f"DROP TYPE {SCHEMA}.alert_triage"))

    init_db(upgraded_engine)
    init_db(upgraded_engine)

    with upgraded_engine.connect() as connection:
        columns = {
            (t, c)
            for t, c in connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema"
                ),
                {"schema": SCHEMA},
            )
        }
        kinds = set(
            connection.scalars(text(f"SELECT unnest(enum_range(NULL::{SCHEMA}.agent_kind))::text"))
        )
    assert {("control", "ops_auto"), ("alerts", "triage"), ("runs", "reviewed_at")} <= columns
    assert "ops_degraded" in kinds
