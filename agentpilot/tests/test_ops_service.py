"""Enquêtes de l'agent Ops dans le service : lancement sur une alerte enregistrée, exécution
enregistrée avec son rapport et son journal, unicité par alerte, budget, vérification
d'isolement, mise à niveau de la base. Faux SDK, vraie base de test."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import control, ops_agent
from agentpilot.app import app
from agentpilot.db import SCHEMA, init_db
from agentpilot.models import AgentKind, Alert, AlertStatus, Run, RunStatus, RunTrigger
from agentpilot.tests.conftest import FakeQuery, HookDrivingQuery, result_message
from agentpilot.tests.test_ops_agent import (
    ALERT,
    NOW,
    context,
    ops_call,
    ops_init,
    ops_sources,
    report,
    run_ops_tool,
)
from agentpilot.worker import AlreadyRunning, abandoned, new_run

# Fixture réutilisée (logs et Prometheus simulés, horloge figée)
__all__ = ["ops_sources"]


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv(control.BUDGET_ENV, "100")
    app.state.session_factory = session_factory
    # Pas de `with` : le lifespan (base principale) n'est pas exécuté
    yield TestClient(app)


@pytest.fixture
def alert(session_factory: sessionmaker[Session]) -> uuid.UUID:
    """L'alerte de `test_ops_agent.ALERT`, enregistrée comme par le webhook d'Alertmanager."""
    with session_factory() as session:
        session.add(
            Alert(
                id=ALERT.id,
                fingerprint="4f1c2b9e8d7a6b5c",
                alertname=ALERT.alertname,
                severity=ALERT.severity,
                route=ALERT.route,
                status=AlertStatus.FIRING,
                summary=ALERT.summary,
                starts_at=ALERT.starts_at,
                ends_at=None,
                first_received_at=NOW,
                last_received_at=NOW,
            )
        )
        session.commit()
    return ALERT.id


def investigation() -> HookDrivingQuery:
    return HookDrivingQuery(
        ops_call("summarize_logs", service="shopsim", since_minutes=5),
        ops_call("query_metrics", metric="shopsim_error_rate", window="1m"),
        output=report(),
        executor=run_ops_tool,
        init=ops_init(),
    )


def test_investigation_is_recorded_with_its_report_and_journal(
    client: TestClient, alert: uuid.UUID, ops_sources: Path
) -> None:
    app.state.query = investigation()

    created = client.post(f"/runs/ops/{alert}")

    assert created.status_code == 202, created.text
    assert created.json()["status"] == "running"
    run = client.get(f"/runs/{created.json()['id']}").json()
    assert run["status"] == "succeeded", run["error"]
    assert run["agent"] == "ops"
    assert run["alert_id"] == str(alert)
    assert run["ticket_id"] is None
    assert run["trigger"] == "manual"
    assert run["output"]["probable_cause"] == "database_errors"
    assert run["output"]["alert_id"] == str(alert)
    assert [(c["tool"], c["outcome"]) for c in run["tool_calls"]] == [
        ("mcp__ops__summarize_logs", "succeeded"),
        ("mcp__ops__query_metrics", "succeeded"),
    ]
    assert run["pending_actions"] == []
    # Retrouvée aussi dans la liste des exécutions
    listed = client.get("/runs").json()
    assert [r["id"] for r in listed if r["agent"] == "ops"] == [run["id"]]


def test_unknown_alert_is_404(client: TestClient) -> None:
    response = client.post(f"/runs/ops/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


def test_one_investigation_at_a_time_per_alert(
    client: TestClient, alert: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    new_run(
        session_factory, AgentKind.OPS, None, RunTrigger.MANUAL, check_budget=False, alert_id=alert
    )

    response = client.post(f"/runs/ops/{alert}")

    assert response.status_code == 409
    with pytest.raises(AlreadyRunning):
        new_run(
            session_factory,
            AgentKind.OPS,
            None,
            RunTrigger.MANUAL,
            check_budget=False,
            alert_id=alert,
        )


def test_investigation_needs_its_own_reserve_in_the_daily_budget(
    client: TestClient,
    alert: uuid.UUID,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0,90 $ restants : assez pour un ticket (0,50 $ réservés), pas pour une enquête (1 $)."""
    monkeypatch.setenv(control.BUDGET_ENV, "0.9")

    response = client.post(f"/runs/ops/{alert}")

    assert response.status_code == 429
    with session_factory() as session:
        assert control.budget_allows(session, agent=AgentKind.SUPPORT)
        assert not control.budget_allows(session, agent=AgentKind.OPS)


def test_running_investigation_counts_its_own_cap(
    alert: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    new_run(
        session_factory, AgentKind.OPS, None, RunTrigger.MANUAL, check_budget=False, alert_id=alert
    )
    new_run(session_factory, AgentKind.SUPPORT, 21, RunTrigger.MANUAL, check_budget=False)

    with session_factory() as session:
        assert control.spent_today(session) == Decimal("1.50")


def test_ops_isolation_check_is_recorded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_agent, "now", lambda: NOW)
    answer = context()
    app.state.query = FakeQuery(ops_init(), result_message(answer))

    response = client.post("/checks/ops")

    assert response.status_code == 200
    run = response.json()
    assert run["agent"] == "ops_check"
    assert run["status"] == "succeeded", run["error"]
    assert run["output"] == answer


def test_ops_isolation_check_fails_on_a_leak(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_agent, "now", lambda: NOW)
    app.state.query = FakeQuery(ops_init(), result_message(context(fault_log_file="chaos.jsonl")))

    run = client.post("/checks/ops").json()

    assert run["status"] == "failed"
    assert "fuite" in run["error"] and "fault_log_file" in run["error"]
    assert run["output"] is None


def test_init_db_upgrades_an_existing_database_for_ops(agentpilot_engine: Engine) -> None:
    """Base d'avant J6.2 : colonne de l'alerte et index ajoutés ; `init_db` deux fois."""
    with agentpilot_engine.begin() as connection:
        # Supprimer la colonne supprime aussi l'index partiel qui l'utilise
        connection.execute(text(f"ALTER TABLE {SCHEMA}.runs DROP COLUMN alert_id"))

    init_db(agentpilot_engine)
    init_db(agentpilot_engine)

    with agentpilot_engine.connect() as connection:
        columns = set(
            connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'runs'"
                ),
                {"schema": SCHEMA},
            )
        )
        indexes = set(
            connection.scalars(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                {"schema": SCHEMA},
            )
        )
        kinds = set(
            connection.scalars(text(f"SELECT unnest(enum_range(NULL::{SCHEMA}.agent_kind))::text"))
        )
    assert "alert_id" in columns
    assert "one_running_run_per_alert" in indexes
    assert {"ops", "ops_check"} <= kinds


def test_interrupted_investigations_are_never_counted_as_abandoned_tickets(
    alert: uuid.UUID, session_factory: sessionmaker[Session]
) -> None:
    """La boucle ne traite que les tickets : des enquêtes interrompues (sans ticket) ne sont ni
    relancées ni affichées comme tickets abandonnés sur la page de pilotage."""
    with session_factory() as session:
        for _ in range(3):
            session.add(
                Run(
                    agent=AgentKind.OPS,
                    alert_id=alert,
                    status=RunStatus.INTERRUPTED,
                    trigger=RunTrigger.MANUAL,
                    created_at=datetime.now(UTC),
                )
            )
        session.commit()

    assert abandoned(session_factory) == []
