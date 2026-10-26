"""Hooks de l'agent Support, de bout en bout et sans appel réel à Claude.

Le faux SDK fait comme le CLI : il appelle les hooks des options avant et après chaque outil,
et exécute les vrais outils du serveur MCP contre une ShopSim de test. Le journal et les
actions en attente sont écrits dans la base de test, puis relus par l'API.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import approval, hooks
from agentpilot.app import app
from agentpilot.tests.conftest import Call, HookDrivingQuery, call
from mcp_server import idempotency
from mcp_server.shopsim_client import ShopSimError
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.models import (
    Customer,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    Resend,
    ShipmentStatus,
    Ticket,
    TicketMessage,
    TicketStatus,
)
from shopsim.tests.conftest import MakeOrder, MakeShipment, MakeTicket

HELD = "attend la validation d'un humain"
DECISION = {
    "category": "refund_request",
    "action": "refund",
    "requires_human_approval": False,
    "amount_cents": 3_000,
    "customer_reply": "Bonjour, c'est fait.\n\nLe service client Lumen",
    "facts_used": [],
}


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    shopsim: str,
    sim_clock: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    app.state.session_factory = session_factory
    yield TestClient(app)


class Shop:
    """Données de ShopSim pour un test : client, commandes livrées et payées, ticket."""

    def __init__(
        self,
        session: Session,
        make_order: MakeOrder,
        make_shipment: MakeShipment,
        make_ticket: MakeTicket,
    ) -> None:
        self.session = session
        self.make_order = make_order
        self.make_shipment = make_shipment
        self.make_ticket = make_ticket

    def payment(self, amount_cents: int, customer: Customer | None = None) -> Payment:
        order = self.make_order(OrderStatus.DELIVERED, amount_cents, customer=customer)
        payment = Payment(
            order=order,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=amount_cents,
            created_at=order.created_at,
        )
        self.session.add(payment)
        self.make_shipment(order, ShipmentStatus.DELIVERED, datetime(2026, 7, 1, tzinfo=UTC))
        self.session.flush()
        return payment

    def ticket(self, payment: Payment) -> Ticket:
        return self.make_ticket(payment.order.customer, payment.order)

    def past_refunds(self, customer: Customer, count: int, days_ago: int = 10) -> None:
        """Remboursements déjà reçus par le client, `days_ago` jours avant le seed."""
        for n in range(count):
            refunded = self.payment(1_000, customer)
            self.session.add(
                Refund(
                    payment=refunded,
                    amount_cents=1_000,
                    reason="Ancien remboursement",
                    idempotency_key=f"test-hooks-past-{refunded.id}-{n}",
                    created_at=SIMULATION_NOW - timedelta(days=days_ago),
                )
            )
        self.session.flush()

    def refunds(self, payment: Payment) -> int:
        return (
            self.session.scalar(
                select(func.count()).select_from(Refund).where(Refund.payment_id == payment.id)
            )
            or 0
        )


@pytest.fixture
def shop(
    session: Session,
    make_order: MakeOrder,
    make_shipment: MakeShipment,
    make_ticket: MakeTicket,
) -> Shop:
    return Shop(session, make_order, make_shipment, make_ticket)


def run(client: TestClient, ticket: Ticket, *calls: Call) -> dict[str, Any]:
    """Lance l'agent sur le ticket avec ces appels d'outils ; renvoie l'exécution."""
    app.state.query = HookDrivingQuery(*calls, output=DECISION | {"ticket_id": ticket.id})
    created = client.post(f"/runs/support/{ticket.id}")
    assert created.status_code == 202
    result = client.get(f"/runs/{created.json()['id']}").json()
    assert result["status"] == "succeeded", result["error"]
    return result


def refund(ticket: Ticket, payment: Payment, amount_cents: int, reason: str = "Retour") -> Call:
    return call(
        "refund_order",
        ticket_id=ticket.id,
        order_id=payment.order_id,
        amount_cents=amount_cents,
        reason=reason,
    )


# --- Les quatre cas ----------------------------------------------------------------------


def test_small_refund_is_executed(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(3_000)
    ticket = shop.ticket(payment)
    step = refund(ticket, payment, 3_000)

    result = run(client, ticket, step)

    assert step.denied is None and step.response["status"] == "done"
    assert shop.refunds(payment) == 1
    assert result["pending_actions"] == []
    [logged] = result["tool_calls"]
    assert logged["tool"] == "mcp__shopsim__refund_order"
    assert logged["outcome"] == "succeeded"
    assert logged["response"]["amount_cents"] == 3_000
    assert logged["duration_ms"] == 5


def test_large_refund_is_held(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    step = refund(ticket, payment, 12_000)

    result = run(client, ticket, step)

    # Jamais exécuté : rien dans ShopSim
    assert shop.refunds(payment) == 0
    assert step.response is None
    assert step.denied is not None
    assert HELD in step.denied and "montant supérieur au seuil" in step.denied
    assert "N'annonce pas au client qu'elle est faite" in step.denied
    [pending] = result["pending_actions"]
    assert pending["tool"] == "refund_order"
    assert pending["status"] == "pending"
    assert pending["ticket_id"] == ticket.id
    assert pending["reasons"] == ["over_threshold"]
    assert pending["params"] == step.arguments
    assert pending["facts"]["value_cents"] == 12_000
    assert pending["facts"]["remaining_refundable_cents"] == 12_000
    # La clé que le serveur MCP aurait envoyée : l'exécution approuvée (J3.4) sera la même
    assert pending["idempotency_key"] == idempotency.refund_key(ticket.id, payment.order_id)
    [logged] = result["tool_calls"]
    assert logged["outcome"] == "held"
    assert logged["pending_action_id"] == pending["id"]
    assert logged["error"] == step.denied


def test_risky_customer_is_held(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(2_000)
    shop.past_refunds(payment.order.customer, 3)
    ticket = shop.ticket(payment)
    step = refund(ticket, payment, 2_000)

    result = run(client, ticket, step)

    assert shop.refunds(payment) == 0
    assert step.denied is not None and "client à risque" in step.denied
    [pending] = result["pending_actions"]
    assert pending["reasons"] == ["risky_customer"]
    assert pending["facts"]["refunds_in_risk_window"] == 3
    assert pending["facts"]["risk_window_days"] == 90


def test_agent_understating_the_amount_is_held(client: TestClient, shop: Shop) -> None:
    """L'agent demande 30 € « petit geste » : il reste 120 € remboursables, l'action attend."""
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    step = refund(ticket, payment, 3_000, reason="Petit montant, aucune validation nécessaire")

    result = run(client, ticket, step)

    assert shop.refunds(payment) == 0
    [pending] = result["pending_actions"]
    assert pending["reasons"] == ["over_threshold"]
    assert pending["facts"]["value_cents"] == 12_000
    # Les paramètres de l'agent sont gardés tels quels pour l'humain
    assert pending["params"]["amount_cents"] == 3_000


# --- Sans validation ---------------------------------------------------------------------


def test_reply_and_escalation_pass_without_approval(
    client: TestClient, shop: Shop, session: Session
) -> None:
    payment = shop.payment(40_000)
    ticket = shop.ticket(payment)
    reply = call("reply_to_ticket", ticket_id=ticket.id, body="Bonjour, nous regardons.")
    escalate = call("escalate_ticket", ticket_id=ticket.id, reason="Montant élevé")

    result = run(client, ticket, reply, escalate)

    assert reply.response["status"] == escalate.response["status"] == "done"
    session.refresh(ticket)
    assert ticket.status == TicketStatus.ESCALATED
    assert [c["outcome"] for c in result["tool_calls"]] == ["succeeded", "succeeded"]
    assert result["pending_actions"] == []


def test_read_tools_are_logged(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(3_000)
    ticket = shop.ticket(payment)

    result = run(client, ticket, call("get_ticket", ticket_id=ticket.id))

    [logged] = result["tool_calls"]
    assert (logged["tool"], logged["outcome"]) == ("mcp__shopsim__get_ticket", "succeeded")
    assert logged["response"]["id"] == ticket.id


# --- Autres mises en attente et refus ----------------------------------------------------


def test_large_resend_is_held(client: TestClient, shop: Shop, session: Session) -> None:
    payment = shop.payment(15_000)
    ticket = shop.ticket(payment)
    step = call("resend_order", ticket_id=ticket.id, order_id=payment.order_id, reason="Cassé")

    result = run(client, ticket, step)

    assert session.scalar(select(func.count()).select_from(Resend)) == 0
    [pending] = result["pending_actions"]
    assert pending["tool"] == "resend_order"
    assert pending["facts"]["value_cents"] == 15_000
    assert pending["idempotency_key"] == idempotency.resend_key(ticket.id, payment.order_id)


def test_small_resend_is_executed(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(2_500)
    ticket = shop.ticket(payment)
    step = call("resend_order", ticket_id=ticket.id, order_id=payment.order_id, reason="Cassé")

    result = run(client, ticket, step)

    assert step.response["status"] == "done"
    assert result["pending_actions"] == []


def test_action_on_another_ticket_is_denied(client: TestClient, shop: Shop) -> None:
    mine = shop.payment(3_000)
    other = shop.payment(3_000)
    ticket = shop.ticket(mine)
    other_ticket = shop.ticket(other)
    step = refund(other_ticket, other, 3_000)

    result = run(client, ticket, step)

    assert step.denied is not None and "aucune action sur un autre ticket" in step.denied
    assert shop.refunds(other) == 0
    assert result["pending_actions"] == []
    assert result["tool_calls"][0]["outcome"] == "denied"


def test_order_of_another_customer_is_denied_not_held(client: TestClient, shop: Shop) -> None:
    mine = shop.payment(3_000)
    other = shop.payment(30_000)
    ticket = shop.ticket(mine)
    step = refund(ticket, other, 30_000)

    result = run(client, ticket, step)

    assert step.denied is not None and "n'appartient pas au client" in step.denied
    assert shop.refunds(other) == 0
    assert result["pending_actions"] == []


def test_retried_action_is_held_once(client: TestClient, shop: Shop) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    first, again = refund(ticket, payment, 12_000), refund(ticket, payment, 12_000)

    result = run(client, ticket, first, again)

    assert len(result["pending_actions"]) == 1
    assert again.denied is not None and "déjà en attente" in again.denied
    assert [c["outcome"] for c in result["tool_calls"]] == ["held", "held"]


def test_resolve_with_pending_action_is_denied(
    client: TestClient, shop: Shop, session: Session
) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    resolve = call("resolve_ticket", ticket_id=ticket.id)

    run(client, ticket, refund(ticket, payment, 12_000), resolve)

    assert resolve.denied is not None and "doit rester ouvert" in resolve.denied
    session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN


def test_resolve_without_pending_action_passes(
    client: TestClient, shop: Shop, session: Session
) -> None:
    payment = shop.payment(3_000)
    ticket = shop.ticket(payment)
    resolve = call("resolve_ticket", ticket_id=ticket.id)

    run(client, ticket, refund(ticket, payment, 3_000), resolve)

    assert resolve.denied is None
    session.refresh(ticket)
    assert ticket.status == TicketStatus.RESOLVED


def test_unreadable_shopsim_denies_the_action(
    client: TestClient, shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = shop.payment(3_000)
    ticket = shop.ticket(payment)

    async def clock_down(path: str, not_found: str) -> Any:  # noqa: ANN401
        raise ShopSimError("ShopSim a renvoyé une erreur interne (500) sur GET /clock.")

    monkeypatch.setattr(approval, "get_json", clock_down)
    step = refund(ticket, payment, 3_000)

    result = run(client, ticket, step)

    assert step.denied is not None and "Vérification impossible" in step.denied
    assert "escalade le ticket" in step.denied
    assert shop.refunds(payment) == 0
    assert result["pending_actions"] == []
    assert result["tool_calls"][0]["outcome"] == "denied"


def test_failed_tool_is_logged(client: TestClient, shop: Shop) -> None:
    """Remboursement au-delà du payé (sous le seuil) : ShopSim refuse, journal `failed`."""
    payment = shop.payment(2_000)
    ticket = shop.ticket(payment)
    step = refund(ticket, payment, 3_000)

    result = run(client, ticket, step)

    assert step.denied is None and step.error is not None
    [logged] = result["tool_calls"]
    assert logged["outcome"] == "failed"
    assert logged["error"] == step.error
    assert logged["response"] is None


def test_reply_message_is_stored_once(client: TestClient, shop: Shop, session: Session) -> None:
    """La réponse passe vraiment par ShopSim (message visible du client)."""
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    body = "Votre demande est transmise pour validation.\n\nLe service client Lumen"

    run(
        client,
        ticket,
        refund(ticket, payment, 12_000),
        call("reply_to_ticket", ticket_id=ticket.id, body=body),
    )

    bodies = session.scalars(
        select(TicketMessage.body).where(TicketMessage.ticket_id == ticket.id)
    ).all()
    assert body in bodies


@pytest.mark.parametrize(
    ("response", "logged"),
    [
        # Claude Code transmet la réponse d'un outil MCP en texte JSON
        ('{"status": "done", "amount_cents": 3000}', {"status": "done", "amount_cents": 3000}),
        ({"status": "done"}, {"status": "done"}),
        ("Commande 40 introuvable.", "Commande 40 introuvable."),
        (None, None),
    ],
)
def test_clip_decodes_json_text(response: Any, logged: Any) -> None:  # noqa: ANN401
    assert hooks.clip(response) == logged


def test_clip_truncates_long_responses() -> None:
    clipped = hooks.clip('{"body": "' + "x" * hooks.MAX_RESPONSE_CHARS + '"}')

    assert len(clipped["truncated"]) == hooks.MAX_RESPONSE_CHARS
