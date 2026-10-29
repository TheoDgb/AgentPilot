"""Métriques Prometheus d'AgentPilot : exécutions, appels d'outils, décisions humaines, état du
pilotage ; étiquettes à valeurs bornées, sans identifiant."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import control, metrics
from agentpilot.api_schemas import ControlStatus
from agentpilot.app import app
from agentpilot.db import make_session_factory
from agentpilot.models import AgentKind, CallOutcome, PauseReason, PendingStatus, RunTrigger
from agentpilot.tests.conftest import (
    FakeQuery,
    Shop,
    call,
    init_message,
    refund,
    run,
)
from agentpilot.tests.test_approvals import ALICE, held_refund, post
from observability.logs import configure
from shopsim.tests.conftest import JsonLogs

Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]
# Étiquettes permises et leurs valeurs : énumérations fermées seulement
ALLOWED_LABELS = {
    "agent": {kind.value for kind in AgentKind},
    "status": {"succeeded", "failed", "interrupted"} | {s.value for s in PendingStatus},
    "trigger": {trigger.value for trigger in RunTrigger},
    "tool": set(metrics.TOOLS) | {metrics.OTHER_TOOL},
    "outcome": {outcome.value for outcome in CallOutcome},
    "decision": {"approved", "rejected"},
    "result": set(metrics.EXECUTION_RESULTS),
    "state": set(metrics.LOOP_STATES) | {"open", "eligible"},
    "reason": {"running", "held", "abandoned", "handled", "proposal", "cooldown"},
    "le": None,
}


def event(name: str, **labels: str) -> float:
    return metrics.EVENTS.get_sample_value(name, labels) or 0.0


def scrape(client: TestClient) -> Samples:
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
        if sample.name.startswith("agentpilot_")
    }


def value(samples: Samples, name: str, **labels: str) -> float:
    return samples[(name, frozenset(labels.items()))]


# --- Exécutions ----------------------------------------------------------------------------


def test_finished_runs_are_counted_with_duration_and_cost(client: TestClient, shop: Shop) -> None:
    labels = {"agent": "support", "status": "succeeded"}
    finished = event("agentpilot_runs_finished_total", **labels, trigger="manual")
    timed = event("agentpilot_run_duration_seconds_count", **labels)
    cost = event("agentpilot_run_cost_usd_total", agent="support")
    payment = shop.payment(3_000)

    run(client, shop.ticket(payment), call("get_order", order_id=payment.order_id))

    assert event("agentpilot_runs_finished_total", **labels, trigger="manual") == finished + 1
    assert event("agentpilot_run_duration_seconds_count", **labels) == timed + 1
    assert event("agentpilot_run_cost_usd_total", agent="support") == pytest.approx(cost + 0.0217)


def test_failed_run_is_counted(client: TestClient) -> None:
    labels = {"agent": "support", "status": "failed", "trigger": "manual"}
    before = event("agentpilot_runs_finished_total", **labels)
    app.state.query = FakeQuery(init_message([*init_message().data["tools"], "Bash"]))

    client.post("/runs/support/21")

    assert event("agentpilot_runs_finished_total", **labels) == before + 1


def test_interrupted_run_has_an_unknown_cost() -> None:
    labels = {"agent": "support", "status": "interrupted", "trigger": "loop"}
    before = event("agentpilot_runs_finished_total", **labels)
    unknown = event("agentpilot_runs_cost_unknown_total", agent="support")

    metrics.run_finished(AgentKind.SUPPORT, RunTrigger.LOOP, None, 12.0)

    assert event("agentpilot_runs_finished_total", **labels) == before + 1
    assert event("agentpilot_runs_cost_unknown_total", agent="support") == unknown + 1


# --- Appels d'outils -----------------------------------------------------------------------


def test_tool_calls_are_counted_by_outcome(client: TestClient, shop: Shop) -> None:
    def calls(tool: str, outcome: str) -> float:
        return event("agentpilot_tool_calls_total", tool=tool, outcome=outcome)

    before = {outcome: calls("refund_order", outcome) for outcome in CallOutcome}

    for amount, requested in ((3_000, 3_000), (12_000, 12_000), (2_000, 3_000)):
        payment = shop.payment(amount)
        ticket = shop.ticket(payment)
        run(client, ticket, refund(ticket, payment, requested))
    # Action sur un autre ticket que celui traité : refusée par le hook
    other = shop.payment(3_000)
    run(client, shop.ticket(shop.payment(3_000)), refund(shop.ticket(other), other, 3_000))

    assert calls("refund_order", "succeeded") == before["succeeded"] + 1
    assert calls("refund_order", "held") == before["held"] + 1
    assert calls("refund_order", "failed") == before["failed"] + 1
    assert calls("refund_order", "denied") == before["denied"] + 1


def test_unknown_tool_names_are_grouped() -> None:
    before = event("agentpilot_tool_calls_total", tool="other", outcome="succeeded")

    metrics.tool_called("mcp__elsewhere__ticket_4521", CallOutcome.SUCCEEDED)

    assert event("agentpilot_tool_calls_total", tool="other", outcome="succeeded") == before + 1
    assert metrics.tool_label("mcp__shopsim__refund_order") == "refund_order"


# --- Décisions humaines et actions en attente ----------------------------------------------


def test_pending_actions_and_decisions(client: TestClient, shop: Shop) -> None:
    approved = event("agentpilot_action_decisions_total", tool="refund_order", decision="approved")
    rejected = event("agentpilot_action_decisions_total", tool="refund_order", decision="rejected")
    executed = event("agentpilot_action_executions_total", tool="refund_order", result="executed")
    _, _, first = held_refund(client, shop)
    _, _, second = held_refund(client, shop)

    pending = scrape(client)
    post(client, f"/approvals/{first}/approve", **ALICE)
    post(client, f"/approvals/{second}/reject", **ALICE, note="Client à risque")
    after = scrape(client)

    status = {"tool": "refund_order", "status": "pending"}
    assert value(pending, "agentpilot_pending_actions", **status) == 2
    assert value(after, "agentpilot_pending_actions", **status) == 0
    assert value(after, "agentpilot_pending_actions", tool="resend_order", status="pending") == 0
    labels = {"tool": "refund_order"}
    assert event("agentpilot_action_decisions_total", **labels, decision="approved") == approved + 1
    assert event("agentpilot_action_decisions_total", **labels, decision="rejected") == rejected + 1
    assert event("agentpilot_action_executions_total", **labels, result="executed") == executed + 1


# --- État du pilotage ----------------------------------------------------------------------


def test_control_state_gauges(
    client: TestClient, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(control.BUDGET_ENV, "7")

    paused = scrape(client)
    control.resume(session_factory, "Alice")
    control.set_emergency(session_factory, True, "Alice")
    running = scrape(client)
    control.pause(session_factory, None, PauseReason.BUDGET)
    budget = scrape(client)
    control.pause(session_factory, None, PauseReason.INFRA, "api_unavailable")
    infra = scrape(client)

    # Premier lancement : boucle en pause
    assert value(paused, "agentpilot_loop_state", state="paused_manual") == 1
    assert value(paused, "agentpilot_loop_state", state="running") == 0
    assert value(paused, "agentpilot_emergency_stop") == 0
    assert value(running, "agentpilot_loop_state", state="running") == 1
    assert value(running, "agentpilot_emergency_stop") == 1
    assert value(budget, "agentpilot_loop_state", state="paused_budget") == 1
    assert sum(value(budget, "agentpilot_loop_state", state=s) for s in metrics.LOOP_STATES) == 1
    assert value(infra, "agentpilot_loop_state", state="paused_infra") == 1
    assert sum(value(infra, "agentpilot_loop_state", state=s) for s in metrics.LOOP_STATES) == 1
    assert value(budget, "agentpilot_budget_daily_usd") == 7
    assert value(budget, "agentpilot_budget_spent_today_usd") == 0
    assert value(budget, "agentpilot_runs_running") == 0
    assert value(budget, "agentpilot_state_up") == 1


def test_unreachable_database_keeps_the_event_metrics(client: TestClient) -> None:
    app.state.session_factory = make_session_factory(
        create_engine("postgresql+psycopg://nobody:nobody@127.0.0.1:1/nothing")
    )

    samples = scrape(client)

    assert value(samples, "agentpilot_state_up") == 0
    assert ("agentpilot_loop_state", frozenset({("state", "running")})) not in samples
    assert any(name == "agentpilot_runs_finished_total" for name, _ in samples)


# --- Collecte ------------------------------------------------------------------------------


@pytest.fixture
def logs(json_logs: JsonLogs) -> JsonLogs:
    """AgentPilot écrit comme en production (`agentpilot.jsonl`)."""
    configure("agentpilot", json_logs.dir, stream=json_logs.stderr)
    return json_logs


def test_metrics_endpoint_is_not_logged(client: TestClient, logs: JsonLogs) -> None:
    client.get("/metrics")
    client.get("/control/status")

    paths = [line.get("path") for line in logs.read("agentpilot")]
    assert "/control/status" in paths
    assert "/metrics" not in paths


def test_labels_have_bounded_values(client: TestClient, shop: Shop) -> None:
    for _ in range(3):
        payment = shop.payment(12_000)
        ticket = shop.ticket(payment)
        run(
            client, ticket, call("get_ticket", ticket_id=ticket.id), refund(ticket, payment, 12_000)
        )

    samples = scrape(client)

    assert samples
    for _, labels in samples:
        for name, label_value in labels:
            assert name in ALLOWED_LABELS, name
            allowed = ALLOWED_LABELS[name]
            assert allowed is None or label_value in allowed, (name, label_value)
            assert not name.endswith("_id")


def test_oldest_pending_action_age() -> None:
    """Jauge de la règle PendingActionTooOld : âge en secondes, 0 sans action en attente."""
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    status = ControlStatus(
        loop_paused=True,
        pause_reason=None,
        paused_by=None,
        paused_at=None,
        emergency_stop=False,
        emergency_by=None,
        emergency_at=None,
        spent_today_usd=Decimal(0),
        daily_budget_usd=Decimal(5),
        running_tickets=[],
        max_concurrent_runs=2,
        waiting_until=None,
        waiting_reason=None,
        abandoned_tickets=[],
        queue_checked_at=None,
        open_tickets=None,
        eligible_tickets=None,
        skipped_tickets={},
    )

    def age(oldest: datetime | None) -> float | None:
        registry = metrics.state_registry(status, {}, now, oldest)
        return registry.get_sample_value("agentpilot_pending_action_oldest_age_seconds")

    assert age(None) == 0
    assert age(now - timedelta(hours=2)) == 7200
