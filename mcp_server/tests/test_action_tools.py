"""Outils MCP d'action : appelés à travers le protocole MCP, avec ShopSim qui tourne."""

from datetime import UTC, datetime
from typing import Any

import pytest
from mcp import Client
from mcp_types import CallToolResult
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcp_server import idempotency
from mcp_server.server import build_server
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.models import (
    ChaosFault,
    ChaosScenario,
    MessageAuthor,
    Order,
    OrderStatus,
    Payment,
    Refund,
    Resend,
    ShipmentStatus,
    Ticket,
    TicketMessage,
    TicketStatus,
)
from shopsim.tests.conftest import MakeOrder, MakePayment, MakeShipment, MakeTicket

pytestmark = pytest.mark.anyio

SERVER = build_server(enable_actions=True)


async def call(name: str, **arguments: Any) -> CallToolResult:  # noqa: ANN401
    async with Client(SERVER) as client:
        return await client.call_tool(name, arguments)


def data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    assert result.is_error, result.structured_content
    return " ".join(block.text for block in result.content if block.type == "text")


def count(session: Session, model: type, *where: Any) -> int:  # noqa: ANN401
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def agent_messages(session: Session, ticket: Ticket) -> list[TicketMessage]:
    return list(
        session.scalars(
            select(TicketMessage).where(
                TicketMessage.ticket_id == ticket.id, TicketMessage.author == MessageAuthor.AGENT
            )
        )
    )


@pytest.fixture
def delivered(make_payment: MakePayment, make_shipment: MakeShipment) -> Payment:
    """Commande livrée et payée (49,90 €) d'un client."""
    payment = make_payment(amount_cents=4_990)
    payment.order.status = OrderStatus.DELIVERED
    make_shipment(payment.order, ShipmentStatus.DELIVERED, datetime(2026, 7, 1, tzinfo=UTC))
    return payment


@pytest.fixture
def ticket(delivered: Payment, make_ticket: MakeTicket) -> Ticket:
    return make_ticket(delivered.order.customer, delivered.order)


# --- Répondre --------------------------------------------------------------------------


async def test_reply_is_sent_once(shopsim: str, session: Session, ticket: Ticket) -> None:
    first = data(await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour Paul."))
    again = data(await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour Paul."))

    assert first["status"] == "done"
    assert again["status"] == "already_done"
    messages = agent_messages(session, ticket)
    assert [m.body for m in messages] == ["Bonjour Paul."]
    # Clé calculée par le serveur : ticket + action + nombre de messages du client
    assert messages[0].idempotency_key == idempotency.reply_key(ticket.id, 1)
    assert messages[0].idempotency_key == f"agentpilot:ticket-{ticket.id}:reply:after-1"


async def test_agent_cannot_choose_the_author(
    shopsim: str, session: Session, ticket: Ticket
) -> None:
    async with Client(SERVER) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert "author" not in tools["reply_to_ticket"].input_schema["properties"]

    # Même en forçant le paramètre, le message n'est jamais celui d'un humain
    await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour Paul.", author="human")

    assert count(session, TicketMessage, TicketMessage.author == MessageAuthor.HUMAN) == 0


async def test_reworded_reply_is_not_sent_twice(
    shopsim: str, session: Session, ticket: Ticket
) -> None:
    await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour Paul.")

    result = await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour Paul, voici.")

    assert f"Action déjà faite sur le ticket {ticket.id} (réponse)" in error_text(result)
    assert len(agent_messages(session, ticket)) == 1


async def test_new_customer_message_allows_a_new_reply(
    shopsim: str, session: Session, ticket: Ticket
) -> None:
    await call("reply_to_ticket", ticket_id=ticket.id, body="Première réponse.")
    # Par la relation : ShopSim partage la session du test et relit ce fil déjà chargé
    ticket.messages.append(
        TicketMessage(
            author=MessageAuthor.CUSTOMER,
            body="Merci, et pour la facture ?",
            created_at=datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
        )
    )
    session.flush()

    result = data(await call("reply_to_ticket", ticket_id=ticket.id, body="Deuxième réponse."))

    assert result["status"] == "done"
    keys = [m.idempotency_key for m in agent_messages(session, ticket)]
    assert keys == [idempotency.reply_key(ticket.id, 1), idempotency.reply_key(ticket.id, 2)]


async def test_reply_to_resolved_ticket_is_refused(
    shopsim: str, session: Session, ticket: Ticket
) -> None:
    ticket.status = TicketStatus.RESOLVED
    session.flush()

    result = await call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour.")

    assert "409" in error_text(result) and "Ticket is resolved" in error_text(result)
    assert agent_messages(session, ticket) == []


# --- Rembourser ------------------------------------------------------------------------


async def test_refund_is_made_once(
    shopsim: str, session: Session, delivered: Payment, ticket: Ticket
) -> None:
    args = {"ticket_id": ticket.id, "order_id": delivered.order_id, "amount_cents": 1_500}

    first = data(await call("refund_order", **args, reason="Geste"))
    again = data(await call("refund_order", **args, reason="Geste"))

    assert (first["status"], again["status"]) == ("done", "already_done")
    assert first["refund_id"] == again["refund_id"]
    refund = session.scalars(select(Refund)).one()
    assert refund.amount_cents == 1_500
    assert refund.idempotency_key == idempotency.refund_key(ticket.id, delivered.order_id)


async def test_refund_with_another_amount_is_not_made_twice(
    shopsim: str, session: Session, delivered: Payment, ticket: Ticket
) -> None:
    args = {"ticket_id": ticket.id, "order_id": delivered.order_id, "reason": "Geste"}
    await call("refund_order", **args, amount_cents=1_500)

    result = await call("refund_order", **args, amount_cents=2_000)

    assert f"Action déjà faite sur le ticket {ticket.id} (remboursement)" in error_text(result)
    assert count(session, Refund) == 1


async def test_refund_above_remaining_amount_is_readable(
    shopsim: str, session: Session, delivered: Payment, ticket: Ticket
) -> None:
    result = await call(
        "refund_order",
        ticket_id=ticket.id,
        order_id=delivered.order_id,
        amount_cents=9_000,
        reason="Trop",
    )

    message = error_text(result)
    assert "422" in message and "Refund exceeds remaining amount" in message
    assert "remaining_cents = 4990" in message
    assert count(session, Refund) == 0


async def test_refund_of_another_customer_order_is_refused(
    shopsim: str,
    session: Session,
    delivered: Payment,
    make_order: MakeOrder,
    make_ticket: MakeTicket,
) -> None:
    stranger_ticket = make_ticket(make_order().customer, delivered.order)

    result = await call(
        "refund_order",
        ticket_id=stranger_ticket.id,
        order_id=delivered.order_id,
        amount_cents=1_000,
        reason="Piège",
    )

    assert f"La commande {delivered.order_id} n'appartient pas au client" in error_text(result)
    assert count(session, Refund) == 0


async def test_refund_during_bad_deploy_is_readable(
    shopsim: str, session: Session, delivered: Payment, ticket: Ticket
) -> None:
    session.add(
        ChaosFault(
            scenario=ChaosScenario.BAD_DEPLOY,
            activated_at=SIMULATION_NOW,
            started_real_at=datetime.now(UTC),
        )
    )
    session.flush()

    result = await call(
        "refund_order",
        ticket_id=ticket.id,
        order_id=delivered.order_id,
        amount_cents=1_000,
        reason="Panne",
    )

    assert "erreur interne (500) sur POST /refunds" in error_text(result)
    assert count(session, Refund) == 0
    # Le serveur répond encore
    assert data(await call("resolve_ticket", ticket_id=ticket.id))["status"] == "done"


# --- Renvoyer --------------------------------------------------------------------------


async def test_resend_is_made_once(
    shopsim: str, session: Session, delivered: Payment, ticket: Ticket
) -> None:
    args = {"ticket_id": ticket.id, "order_id": delivered.order_id, "reason": "Défectueux"}

    first = data(await call("resend_order", **args))
    again = data(await call("resend_order", **args))

    assert (first["status"], again["status"]) == ("done", "already_done")
    assert first["replacement_order_id"] == again["replacement_order_id"]
    assert first["tracking_number"].startswith("RPD")
    resend = session.scalars(select(Resend)).one()
    assert resend.idempotency_key == idempotency.resend_key(ticket.id, delivered.order_id)
    replacement = session.get(Order, first["replacement_order_id"])
    assert replacement is not None and replacement.total_cents == 0


async def test_resend_of_parcel_in_transit_is_refused(
    shopsim: str,
    session: Session,
    make_order: MakeOrder,
    make_shipment: MakeShipment,
    make_ticket: MakeTicket,
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    make_shipment(order, ShipmentStatus.IN_TRANSIT)
    in_transit_ticket = make_ticket(order.customer, order)

    result = await call(
        "resend_order", ticket_id=in_transit_ticket.id, order_id=order.id, reason="Trop tôt"
    )

    assert "Order cannot be resent" in error_text(result)
    assert count(session, Resend) == 0


async def test_resend_of_another_customer_order_is_refused(
    shopsim: str,
    session: Session,
    delivered: Payment,
    make_order: MakeOrder,
    make_ticket: MakeTicket,
) -> None:
    stranger_ticket = make_ticket(make_order().customer)

    result = await call(
        "resend_order", ticket_id=stranger_ticket.id, order_id=delivered.order_id, reason="Piège"
    )

    assert "n'appartient pas au client" in error_text(result)
    assert count(session, Resend) == 0


# --- Escalader et résoudre -------------------------------------------------------------


async def test_escalation_is_made_once(shopsim: str, session: Session, ticket: Ticket) -> None:
    first = data(await call("escalate_ticket", ticket_id=ticket.id, reason="Commande d'un tiers"))
    again = data(await call("escalate_ticket", ticket_id=ticket.id, reason="Commande d'un tiers"))
    other = await call("escalate_ticket", ticket_id=ticket.id, reason="Autre motif")

    assert (first["status"], again["status"]) == ("done", "already_done")
    assert first["ticket_status"] == "escalated"
    assert "Ticket already escalated with a different reason" in error_text(other)
    session.refresh(ticket)
    assert ticket.escalation_reason == "Commande d'un tiers"


async def test_resolution_is_made_once(shopsim: str, session: Session, ticket: Ticket) -> None:
    first = data(await call("resolve_ticket", ticket_id=ticket.id))
    again = data(await call("resolve_ticket", ticket_id=ticket.id))

    assert (first["status"], again["status"]) == ("done", "already_done")
    session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED


# --- Erreurs communes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("reply_to_ticket", {"body": "Bonjour."}),
        ("refund_order", {"order_id": 1, "amount_cents": 100, "reason": "x"}),
        ("resend_order", {"order_id": 1, "reason": "x"}),
        ("escalate_ticket", {"reason": "x"}),
        ("resolve_ticket", {}),
    ],
)
async def test_missing_ticket_is_readable(
    shopsim: str, tool: str, arguments: dict[str, Any]
) -> None:
    result = await call(tool, ticket_id=999_999, **arguments)

    assert "Ticket 999999 introuvable" in error_text(result)


async def test_missing_order_is_readable(shopsim: str, ticket: Ticket) -> None:
    result = await call("resend_order", ticket_id=ticket.id, order_id=999_999, reason="x")

    assert "Commande 999999 introuvable" in error_text(result)
