"""Horloge de simulation : seule source de temps de ShopSim."""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from shopsim.app import clock
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.models import (
    ChaosFault,
    ChaosScenario,
    OrderStatus,
    Refund,
    Resend,
    ShipmentStatus,
    TicketMessage,
)
from shopsim.seed import seed
from shopsim.tests.conftest import MakeCustomer, MakePayment, MakeShipment, MakeTicket, SimClock

SHOPSIM_DIR = Path(__file__).resolve().parents[1]
# Lectures de l'horloge système interdites hors de clock.py
FORBIDDEN_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("func", "now"),
}


def _system_clock_calls(path: Path, root: Path = SHOPSIM_DIR) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in FORBIDDEN_CALLS
        ):
            found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_only_the_clock_reads_system_time() -> None:
    sources = [
        path
        for path in SHOPSIM_DIR.rglob("*.py")
        if "tests" not in path.parts and path.name != "clock.py"
    ]
    assert sources, "aucun fichier analysé"

    offenders = [call for path in sources for call in _system_clock_calls(path)]

    assert offenders == [], f"Utiliser clock.now(session) : {offenders}"


def test_the_scan_detects_system_time(tmp_path: Path) -> None:
    """Garde-fou du test précédent : il repère bien chaque forme interdite."""
    source = tmp_path / "bad.py"
    source.write_text(
        "datetime.now(UTC)\ndatetime.utcnow()\ndate.today()\ntime.time()\nfunc.now()\n",
        encoding="utf-8",
    )

    assert len(_system_clock_calls(source, root=tmp_path)) == len(FORBIDDEN_CALLS)


# --- Écoulement du temps ---------------------------------------------------------------


def test_simulation_starts_at_simulation_now_and_follows_real_time(
    session: Session, sim_clock: SimClock
) -> None:
    assert clock.now(session) == SIMULATION_NOW

    sim_clock.elapse(timedelta(minutes=90))

    assert clock.now(session) == SIMULATION_NOW + timedelta(minutes=90)


def test_new_seed_resets_the_simulation(session: Session, sim_clock: SimClock) -> None:
    sim_clock.elapse(timedelta(days=3))
    assert clock.now(session) == SIMULATION_NOW + timedelta(days=3)

    clock.start(session)
    session.flush()

    assert clock.now(session) == SIMULATION_NOW


def test_seed_anchors_the_clock(session: Session, sim_clock: SimClock) -> None:
    sim_clock.elapse(timedelta(days=2))

    seed(session)
    session.flush()

    assert clock.now(session) == SIMULATION_NOW


def test_without_anchor_time_starts_with_the_process(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "real_now", lambda: clock._PROCESS_START + timedelta(seconds=30))

    assert clock.now(session) == SIMULATION_NOW + timedelta(seconds=30)


def test_never_before_simulation_start(session: Session, sim_clock: SimClock) -> None:
    sim_clock.elapse(timedelta(hours=-5))

    assert clock.now(session) == SIMULATION_NOW


# --- Écritures datées par l'horloge de simulation --------------------------------------

ELAPSED = timedelta(minutes=7)
EXPECTED = SIMULATION_NOW + ELAPSED


def test_writes_are_dated_by_the_simulation_clock(
    client: TestClient,
    session: Session,
    sim_clock: SimClock,
    make_payment: MakePayment,
    make_shipment: MakeShipment,
    make_ticket: MakeTicket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", "test-chaos-token")
    payment = make_payment()
    order = payment.order
    order.status = OrderStatus.DELIVERED
    make_shipment(order, ShipmentStatus.DELIVERED)
    ticket = make_ticket(order.customer, order)
    sim_clock.elapse(ELAPSED)

    refund = client.post(
        "/refunds",
        json={"payment_id": payment.id, "amount_cents": 100, "reason": "Test horloge"},
        headers={"Idempotency-Key": "clock-refund"},
    )
    reply = client.post(
        f"/tickets/{ticket.id}/reply",
        json={"body": "Bonjour"},
        headers={"Idempotency-Key": "clock-reply"},
    )
    resend = client.post(
        f"/orders/{order.id}/resend",
        json={"reason": "Test horloge"},
        headers={"Idempotency-Key": "clock-resend"},
    )
    fault = client.post("/chaos/bad_deploy", headers={"X-Chaos-Token": "test-chaos-token"})

    assert [r.status_code for r in (refund, reply, resend, fault)] == [201, 201, 201, 201]
    assert session.scalars(select(Refund.created_at)).one() == EXPECTED
    agent_message = session.scalars(
        select(TicketMessage).where(TicketMessage.idempotency_key == "clock-reply")
    ).one()
    assert agent_message.created_at == EXPECTED
    created = session.scalars(select(Resend)).one()
    assert created.created_at == EXPECTED
    assert created.replacement_order.created_at == EXPECTED
    assert created.replacement_order.shipment is not None
    assert (
        created.replacement_order.shipment.estimated_delivery
        == (EXPECTED + timedelta(days=3)).date()
    )
    fault_row = session.get(ChaosFault, ChaosScenario.BAD_DEPLOY)
    assert fault_row is not None
    assert fault_row.activated_at == EXPECTED


def test_tenure_follows_the_simulation_clock(
    client: TestClient, make_customer: MakeCustomer, sim_clock: SimClock
) -> None:
    customer = make_customer(created_at=SIMULATION_NOW - timedelta(days=10))
    assert client.get(f"/customers/{customer.id}").json()["tenure_days"] == 10

    sim_clock.elapse(timedelta(days=2))

    assert client.get(f"/customers/{customer.id}").json()["tenure_days"] == 12
