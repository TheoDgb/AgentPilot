"""API du service AgentPilot : exécutions lancées en tâche de fond et enregistrées en base."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.app import app, mark_interrupted
from agentpilot.db import SCHEMA
from agentpilot.models import AgentKind, Run, RunStatus
from agentpilot.support_agent import expected_context
from agentpilot.tests.conftest import (
    TODAY,
    FakeQuery,
    HookDrivingQuery,
    call,
    init_message,
    result_message,
)
from shopsim.app.db import Base as ShopSimBase

DECISION = {
    "ticket_id": 21,
    "category": "refund_request",
    "action": "refuse",
    "requires_human_approval": False,
    "amount_cents": None,
    "customer_reply": "Bonjour, le délai est dépassé.\n\nLe service client Lumen",
    "escalation_reason": None,
    "facts_used": [{"tool": "get_shipment", "field": "days_since_delivery", "value": 46}],
}


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    app.state.session_factory = session_factory
    # Pas de `with` : le lifespan (base principale) n'est pas exécuté
    yield TestClient(app)


def use_query(fake: FakeQuery) -> FakeQuery:
    app.state.query = fake
    return fake


async def fake_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"order_id": 60, "status": "delivered"}


def test_support_run_is_recorded(client: TestClient) -> None:
    use_query(
        HookDrivingQuery(call("get_shipment", order_id=60), output=DECISION, executor=fake_tool)
    )

    created = client.post("/runs/support/21")

    assert created.status_code == 202
    assert created.json()["status"] == "running"
    run = client.get(f"/runs/{created.json()['id']}").json()
    assert run["status"] == "succeeded"
    assert run["agent"] == "support"
    assert run["ticket_id"] == 21
    assert run["output"] == DECISION
    assert run["error"] is None
    assert run["simulation_date"] == "2026-07-06"
    assert run["model"] == "claude-sonnet-5-5"
    assert float(run["total_cost_usd"]) == 0.0217
    assert (run["duration_ms"], run["num_turns"]) == (8_000, 4)
    assert len(run["available_tools"]) == 14
    # Journal écrit par les hooks
    assert run["tool_calls"] == [
        {
            "seq": 1,
            "tool": "mcp__shopsim__get_shipment",
            "input": {"order_id": 60},
            "outcome": "succeeded",
            "response": {"order_id": 60, "status": "delivered"},
            "error": None,
            "duration_ms": 5,
            "pending_action_id": None,
            # Auteur de l'appel (J6.4) : None, l'agent principal (Support n'a pas de subagent)
            "agent": None,
        }
    ]
    assert run["pending_actions"] == []
    assert run["finished_at"] is not None


def test_failed_run_is_recorded_with_its_reason(client: TestClient) -> None:
    use_query(FakeQuery(init_message([*init_message().data["tools"], "Bash"])))

    created = client.post("/runs/support/21").json()

    run = client.get(f"/runs/{created['id']}").json()
    assert run["status"] == "failed"
    assert "interdits : Bash" in run["error"]
    assert run["output"] is None


def test_unexpected_error_is_recorded(client: TestClient) -> None:
    def broken(**kwargs: object) -> None:
        raise RuntimeError("boom")

    app.state.query = broken

    created = client.post("/runs/support/21").json()

    run = client.get(f"/runs/{created['id']}").json()
    assert run["status"] == "failed"
    assert "Erreur interne d'AgentPilot" in run["error"]


def test_list_runs_by_ticket(client: TestClient) -> None:
    use_query(FakeQuery(init_message(), result_message(DECISION)))
    first = client.post("/runs/support/21").json()["id"]
    second = client.post("/runs/support/21").json()["id"]
    client.post("/runs/support/22")

    runs = client.get("/runs", params={"ticket_id": 21}).json()

    assert {r["id"] for r in runs} == {first, second}


def test_unknown_run_returns_404(client: TestClient) -> None:
    response = client.get("/runs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}


def test_support_check(client: TestClient) -> None:
    answer = expected_context(TODAY).model_dump()
    use_query(FakeQuery(init_message(), result_message(answer)))

    response = client.post("/checks/support")

    assert response.status_code == 200
    run = response.json()
    assert run["agent"] == "support_check"
    assert run["status"] == "succeeded"
    assert run["output"] == answer


def test_running_runs_are_interrupted_at_startup(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            Run(
                agent=AgentKind.SUPPORT,
                ticket_id=21,
                status=RunStatus.RUNNING,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

        assert mark_interrupted(session) == 1

        run = session.scalars(select(Run)).one()
        assert run.status == RunStatus.INTERRUPTED
        assert run.finished_at is not None


def test_shopsim_seed_does_not_touch_agentpilot(session_factory: sessionmaker[Session]) -> None:
    """Le seed de ShopSim supprime ses propres tables, jamais le schéma `agentpilot`."""
    assert all(table.schema is None for table in ShopSimBase.metadata.tables.values())
    with session_factory() as session:
        connection = session.connection()
        ShopSimBase.metadata.drop_all(connection)

        assert "runs" in inspect(connection).get_table_names(schema=SCHEMA)
