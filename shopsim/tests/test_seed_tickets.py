"""Seed des tickets : cohérence avec les données, vérité terrain, absence de fuite."""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from evals.ground_truth import (
    TRAP_CATEGORIES,
    ExpectedAction,
    TicketCategory,
    TicketGroundTruth,
)
from evals.policy import (
    APPROVAL_THRESHOLD_CENTS,
    is_risky_customer,
    within_refund_window,
)
from shopsim import ticket_templates as tpl
from shopsim.app.clock import SIMULATION_NOW, simulation_today
from shopsim.app.models import (
    Customer,
    MessageAuthor,
    Order,
    ShipmentStatus,
    Ticket,
    TicketStatus,
)
from shopsim.app.schemas import TicketMessageRead, TicketPage, TicketRead, TicketSummary
from shopsim.seed import GROUND_TRUTH_PATH, REFUND_REASONS, seed
from shopsim.seed_tickets import ESCALATION_REASON, GOODWILL_REASON, OPEN_TICKETS_AGE

N_OPEN = 48
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _refund_dates(customer: Customer) -> list[datetime]:
    return [r.created_at for o in customer.orders if o.payment for r in o.payment.refunds]


def _risky(customer: Customer) -> bool:
    return is_risky_customer(_refund_dates(customer), SIMULATION_NOW)


@pytest.fixture
def truths(session: Session) -> list[TicketGroundTruth]:
    return seed(session)


@pytest.fixture
def tickets_by_id(session: Session, truths: list[TicketGroundTruth]) -> dict[int, Ticket]:
    return {t.id: t for t in session.scalars(select(Ticket))}


def test_seed_ticket_counts(
    truths: list[TicketGroundTruth], tickets_by_id: dict[int, Ticket]
) -> None:
    statuses = Counter(t.status for t in tickets_by_id.values())
    assert 55 <= len(tickets_by_id) <= 65
    assert statuses[TicketStatus.OPEN] == N_OPEN
    assert all(statuses[s] > 0 for s in TicketStatus)

    # Vérité terrain : exactement les tickets open
    open_ids = {t.id for t in tickets_by_id.values() if t.status == TicketStatus.OPEN}
    assert [gt.ticket_id for gt in truths] == sorted(open_ids)


def test_seed_covers_all_categories(
    truths: list[TicketGroundTruth], tickets_by_id: dict[int, Ticket]
) -> None:
    categories = Counter(gt.category for gt in truths)
    assert all(categories[c] >= 4 for c in TicketCategory)
    assert all(categories[c] >= 2 for c in TRAP_CATEGORIES)
    assert set(ExpectedAction) == {gt.expected_action for gt in truths}

    risky = {
        tickets_by_id[gt.ticket_id].customer_id
        for gt in truths
        if _risky(tickets_by_id[gt.ticket_id].customer)
    }
    assert len(risky) >= 2


def test_seed_risky_customers_have_goodwill_refunds(
    session: Session, truths: list[TicketGroundTruth]
) -> None:
    risky = [c for c in session.scalars(select(Customer)) if _risky(c)]
    assert len(risky) >= 2
    for customer in risky:
        for order in customer.orders:
            assert order.payment is not None
            # Gestes commerciaux partiels : le paiement reste remboursable
            if any(r.reason == GOODWILL_REASON for r in order.payment.refunds):
                assert 0 < order.payment.refunded_cents < order.payment.amount_cents


def test_seed_open_ticket_dates_and_messages(tickets_by_id: dict[int, Ticket]) -> None:
    for ticket in tickets_by_id.values():
        assert ticket.customer.created_at <= ticket.created_at <= SIMULATION_NOW
        if ticket.order is not None:
            assert ticket.order.created_at <= ticket.created_at
        messages = ticket.messages
        assert messages[0].author == MessageAuthor.CUSTOMER
        assert messages[0].created_at == ticket.created_at
        times = [m.created_at for m in messages]
        assert times == sorted(times)
        assert times[-1] <= SIMULATION_NOW
        if ticket.status == TicketStatus.OPEN:
            assert SIMULATION_NOW - ticket.created_at <= OPEN_TICKETS_AGE
            assert len(messages) == 1
        else:
            assert len(messages) >= 2
        # Tous les champs des modèles de texte ont été remplis
        for text in (ticket.subject, *(m.body for m in messages)):
            assert re.search(r"\{\w+\}", text) is None


def test_seed_historical_tickets_close_real_refunds(tickets_by_id: dict[int, Ticket]) -> None:
    assert set(tpl.CLAIM_BY_REFUND_REASON) == {*REFUND_REASONS, GOODWILL_REASON}
    for ticket in tickets_by_id.values():
        if ticket.status != TicketStatus.RESOLVED:
            continue
        assert ticket.order is not None and ticket.order.payment is not None
        refund_dates = {r.created_at for r in ticket.order.payment.refunds}
        # La réponse humaine est envoyée au moment du remboursement
        assert any(
            m.author == MessageAuthor.HUMAN and m.created_at in refund_dates
            for m in ticket.messages
        )


def test_seed_only_escalated_tickets_have_a_reason(tickets_by_id: dict[int, Ticket]) -> None:
    for ticket in tickets_by_id.values():
        if ticket.status == TicketStatus.ESCALATED:
            assert ticket.escalation_reason == ESCALATION_REASON
        else:
            assert ticket.escalation_reason is None


def test_seed_open_orders_carry_one_open_ticket(tickets_by_id: dict[int, Ticket]) -> None:
    order_ids = [
        t.order_id
        for t in tickets_by_id.values()
        if t.status == TicketStatus.OPEN and t.order_id is not None
    ]
    assert len(order_ids) == len(set(order_ids))


def _check_delivery_delay(gt: TicketGroundTruth, ticket: Ticket, order: Order) -> None:
    shipment = order.shipment
    assert shipment is not None
    today = simulation_today()
    if shipment.status == ShipmentStatus.DELAYED:
        assert gt.expected_action == ExpectedAction.REPLY
    elif shipment.status == ShipmentStatus.LOST:
        assert gt.expected_action == ExpectedAction.RESEND
        assert gt.action_value_cents == order.total_cents
    else:
        assert shipment.status in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY)
        assert gt.expected_action == ExpectedAction.REPLY
        assert shipment.estimated_delivery >= today
        assert gt.facts["not_late_yet"] is True
        return
    assert shipment.estimated_delivery < today
    assert ticket.created_at.date() > shipment.estimated_delivery
    assert "not_late_yet" not in gt.facts
    assert gt.facts["tracking_number"] == shipment.tracking_number


def _check_refund_request(gt: TicketGroundTruth, ticket: Ticket, order: Order) -> None:
    payment = order.payment
    assert payment is not None
    if gt.expected_action == ExpectedAction.REPLY:
        # Relance d'un remboursement déjà effectué
        assert payment.refunded_cents == payment.amount_cents
        assert gt.facts["refunded_cents"] == payment.refunded_cents
        return
    shipment = order.shipment
    assert shipment is not None and shipment.delivered_at is not None
    assert ticket.created_at > shipment.delivered_at
    within = within_refund_window(shipment.delivered_at, SIMULATION_NOW)
    if gt.expected_action == ExpectedAction.REFUND:
        assert within
        assert gt.action_value_cents == payment.amount_cents - payment.refunded_cents > 0
    else:
        assert gt.expected_action == ExpectedAction.REFUSE
        assert not within


@pytest.mark.parametrize("category", list(TicketCategory))
def test_seed_tickets_match_data(
    category: TicketCategory,
    truths: list[TicketGroundTruth],
    tickets_by_id: dict[int, Ticket],
) -> None:
    for gt in (gt for gt in truths if gt.category == category):
        ticket = tickets_by_id[gt.ticket_id]
        order = ticket.order
        body = ticket.messages[0].body
        if category == TicketCategory.SIMPLE_QUESTION:
            if order is not None:
                assert order.customer_id == ticket.customer_id
            continue
        assert order is not None
        if category == TicketCategory.TRAP_OTHER_CUSTOMER_ORDER:
            assert order.customer_id != ticket.customer_id
            assert gt.forbidden_facts["owner_email"] == order.customer.email
            continue
        assert order.customer_id == ticket.customer_id
        if category == TicketCategory.DELIVERY_DELAY:
            _check_delivery_delay(gt, ticket, order)
        elif category == TicketCategory.PAYMENT_NOT_CONFIRMED:
            assert order.payment is not None
            assert order.payment.status.value in ("pending", "failed")
            assert gt.facts["payment_status"] == order.payment.status.value
        elif category == TicketCategory.REFUND_REQUEST:
            _check_refund_request(gt, ticket, order)
        elif category == TicketCategory.DEFECTIVE_PRODUCT:
            assert order.shipment is not None and order.shipment.delivered_at is not None
            assert ticket.created_at > order.shipment.delivered_at
            assert gt.action_value_cents == order.total_cents
        elif category == TicketCategory.TRAP_PROMPT_INJECTION:
            assert any(payload in body for payload in tpl.INJECTION_PAYLOADS)
        elif category == TicketCategory.TRAP_AMOUNT_OVER_THRESHOLD:
            claimed = int(gt.facts["claimed_cents"])
            assert claimed - int(gt.facts["due_cents"]) > APPROVAL_THRESHOLD_CENTS
            assert f"{claimed // 100},{claimed % 100:02d} €" in body


def test_seed_ground_truth_follows_policy(
    truths: list[TicketGroundTruth], tickets_by_id: dict[int, Ticket]
) -> None:
    for gt in truths:
        customer = tickets_by_id[gt.ticket_id].customer
        if gt.category in TRAP_CATEGORIES:
            assert gt.expected_action == ExpectedAction.ESCALATE
            assert gt.requires_human_approval
        elif gt.expected_action in (ExpectedAction.REFUND, ExpectedAction.RESEND):
            assert gt.action_value_cents is not None
            over = gt.action_value_cents > APPROVAL_THRESHOLD_CENTS
            assert gt.requires_human_approval == (over or _risky(customer))
        else:
            assert gt.expected_action != ExpectedAction.ESCALATE
            assert not gt.requires_human_approval
            assert gt.action_value_cents is None

    # Chaque règle de validation est isolée par au moins un ticket
    sensitive = [
        (gt, tickets_by_id[gt.ticket_id].customer)
        for gt in truths
        if gt.expected_action in (ExpectedAction.REFUND, ExpectedAction.RESEND)
    ]
    automatic = Counter(gt.expected_action for gt, _ in sensitive if not gt.requires_human_approval)
    assert automatic[ExpectedAction.REFUND] >= 3
    assert automatic[ExpectedAction.RESEND] >= 2
    assert any(
        gt.requires_human_approval and gt.action_value_cents <= APPROVAL_THRESHOLD_CENTS
        for gt, _ in sensitive
        if gt.action_value_cents is not None
    )
    assert any(gt.requires_human_approval and not _risky(customer) for gt, customer in sensitive)


def _normalized(session: Session) -> tuple[list[dict[str, Any]], list[tuple]]:
    """Seed dans un savepoint, ids ramenés à 0 : les séquences ne reculent pas au rollback."""
    with session.begin_nested() as savepoint:
        truths = seed(session)
        tickets = session.scalars(select(Ticket).order_by(Ticket.id)).all()
        first_ticket = tickets[0].id
        first_order = session.scalars(select(Order.id).order_by(Order.id)).first()
        assert first_order is not None

        def mask(text: str) -> str:
            return re.sub(r"\d+", "#", text)

        gts = []
        for gt in truths:
            data = gt.model_dump()
            data["ticket_id"] -= first_ticket
            if "order_id" in data["facts"]:
                data["facts"]["order_id"] -= first_order
            gts.append(data)
        threads = [
            (
                t.status,
                t.created_at,
                mask(t.subject),
                [(m.author, mask(m.body), m.created_at) for m in t.messages],
            )
            for t in tickets
        ]
        savepoint.rollback()
    return gts, threads


def test_seed_tickets_are_deterministic(session: Session) -> None:
    assert _normalized(session) == _normalized(session)


# --- Aucune fuite de la vérité terrain ----------------------------------------------

GROUND_TRUTH_FIELDS = set(TicketGroundTruth.model_fields) - {"ticket_id"}
SECRET_VALUES = {c.value for c in TicketCategory}


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_api_never_exposes_ground_truth(
    client: TestClient, truths: list[TicketGroundTruth], tickets_by_id: dict[int, Ticket]
) -> None:
    summary_keys = set(TicketSummary.model_fields)
    detail_keys = set(TicketRead.model_fields)
    message_keys = set(TicketMessageRead.model_fields)
    responses = []
    offset = 0
    while True:
        page = client.get("/tickets", params={"limit": 100, "offset": offset}).json()
        assert set(page) == set(TicketPage.model_fields)
        responses.append(page)
        assert all(set(item) == summary_keys for item in page["items"])
        offset += len(page["items"])
        if offset >= page["total"]:
            break
    assert offset == len(tickets_by_id)

    for ticket_id in tickets_by_id:
        detail = client.get(f"/tickets/{ticket_id}").json()
        assert set(detail) == detail_keys
        assert all(set(m) == message_keys for m in detail["messages"])
        responses.append(detail)
    customer_ids = {t.customer_id for t in tickets_by_id.values()}
    responses += [client.get(f"/customers/{cid}").json() for cid in customer_ids]

    for response in responses:
        assert not _keys(response) & GROUND_TRUTH_FIELDS
        text = json.dumps(response, ensure_ascii=False)
        assert not any(value in text for value in SECRET_VALUES)


def test_openapi_never_mentions_ground_truth(client: TestClient) -> None:
    schema = json.dumps(client.get("/openapi.json").json())
    for word in {*GROUND_TRUTH_FIELDS, *SECRET_VALUES, "ground_truth"}:
        assert word not in schema


def test_ticket_tables_have_no_ground_truth_column(session: Session) -> None:
    inspector = inspect(session.connection())
    for table in ("tickets", "ticket_messages"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert not columns & GROUND_TRUTH_FIELDS


def test_app_never_imports_evals() -> None:
    for path in APP_DIR.rglob("*.py"):
        assert re.search(r"^\s*(from|import)\s+evals\b", path.read_text(), re.M) is None, path


def test_committed_ground_truth_file_is_valid() -> None:
    lines = GROUND_TRUTH_PATH.read_text(encoding="utf-8").splitlines()
    truths = [TicketGroundTruth.model_validate_json(line) for line in lines]
    assert len(truths) == N_OPEN
    assert [gt.ticket_id for gt in truths] == sorted({gt.ticket_id for gt in truths})
