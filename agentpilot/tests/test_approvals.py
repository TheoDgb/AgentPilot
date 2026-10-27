"""Pages et décisions d'approbation, sans appel réel à Claude.

Les actions en attente sont créées par le vrai chemin : faux SDK, hooks, base de test. Les
décisions sont exécutées contre la ShopSim de test, par HTTP.
"""

import asyncio
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import decisions
from agentpilot.approval import REFUND, RESEND
from agentpilot.db import make_session_factory
from agentpilot.decisions import Notice
from agentpilot.models import AgentKind, PendingAction, PendingStatus, Run, RunStatus
from agentpilot.tests.conftest import DECISION, Shop, call, refund, run
from mcp_server import idempotency
from mcp_server.shopsim_client import ShopSimUnavailable
from shopsim.app.models import (
    MessageAuthor,
    Payment,
    Refund,
    Resend,
    ShipmentStatus,
    Ticket,
    TicketMessage,
    TicketStatus,
)

ALICE = {"name": "Alice"}


def held_refund(
    client: TestClient, shop: Shop, amount: int = 12_000, requested: int | None = None
) -> tuple[Ticket, Payment, str]:
    """Remboursement mis en attente par le hook ; renvoie le ticket, le paiement, l'action."""
    payment = shop.payment(amount)
    ticket = shop.ticket(payment)
    result = run(client, ticket, refund(ticket, payment, requested or amount))
    [pending] = result["pending_actions"]
    return ticket, payment, pending["id"]


def action(factory: sessionmaker[Session], action_id: str) -> PendingAction:
    found = decisions.load(factory, uuid.UUID(action_id))
    assert found is not None
    return found


def notices(response: Response) -> list[str]:
    assert response.status_code == 303, response.text
    query = response.headers["location"].split("?", 1)[1]
    return [part.split("=", 1)[1] for part in query.split("&") if part.startswith("notice=")]


def decision_messages(session: Session, ticket: Ticket) -> list[TicketMessage]:
    """Messages envoyés au client par l'humain (clé de décision), pas par l'agent."""
    return list(
        session.scalars(
            select(TicketMessage).where(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.idempotency_key.like("%:reply:decision-%"),
            )
        )
    )


def post(client: TestClient, path: str, **form: Any) -> Response:  # noqa: ANN401
    return client.post(path, data=form, follow_redirects=False)


# --- Approbation -------------------------------------------------------------------------


def test_approval_is_executed_once(
    client: TestClient, shop: Shop, session_factory: sessionmaker[Session]
) -> None:
    ticket, payment, action_id = held_refund(client, shop)

    first = post(client, f"/approvals/{action_id}/approve", **ALICE)
    again = post(client, f"/approvals/{action_id}/approve", name="Bob")

    assert notices(first) == [Notice.EXECUTED]
    assert notices(again) == [Notice.ALREADY_DECIDED]
    assert shop.refunds(payment) == 1
    done = action(session_factory, action_id)
    assert done.status is PendingStatus.EXECUTED
    assert done.decided_by == "Alice" and done.decided_at is not None
    assert done.executed_at is not None and done.result["amount_cents"] == 12_000


def test_approval_uses_the_recorded_key(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    ticket, payment, action_id = held_refund(client, shop)

    post(client, f"/approvals/{action_id}/approve", **ALICE)

    key = session.scalar(select(Refund.idempotency_key).where(Refund.payment_id == payment.id))
    assert key == action(session_factory, action_id).idempotency_key
    assert key == idempotency.refund_key(ticket.id, payment.order_id)


def test_approval_requires_a_name(
    client: TestClient, shop: Shop, session_factory: sessionmaker[Session]
) -> None:
    _, payment, action_id = held_refund(client, shop)

    response = post(client, f"/approvals/{action_id}/approve", name="  ")

    assert response.status_code == 422
    assert "obligatoire" in response.text
    assert action(session_factory, action_id).status is PendingStatus.PENDING
    assert shop.refunds(payment) == 0


# --- Refus -------------------------------------------------------------------------------


def test_rejection_requires_a_reason(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    ticket, payment, action_id = held_refund(client, shop)
    message = "Bonjour,\n\nNous ne pouvons pas donner suite.\n\nLe service client Lumen"

    without = post(
        client,
        f"/approvals/{action_id}/reject",
        name="Bob",
        note="",
        message=message,
        send_message="true",
    )
    assert without.status_code == 422
    assert "motif" in without.text
    assert action(session_factory, action_id).status is PendingStatus.PENDING

    response = post(
        client,
        f"/approvals/{action_id}/reject",
        name="Bob",
        note="Retour hors délai",
        message=message,
        send_message="true",
    )

    assert notices(response) == [Notice.REJECTED, Notice.MESSAGE_SENT]
    done = action(session_factory, action_id)
    assert done.status is PendingStatus.REJECTED
    assert (done.decided_by, done.decision_note) == ("Bob", "Retour hors délai")
    assert shop.refunds(payment) == 0
    [sent] = decision_messages(session, ticket)
    assert sent.body == message and sent.author is MessageAuthor.HUMAN
    assert sent.idempotency_key == idempotency.decision_reply_key(ticket.id, action_id)


# --- Double approbation ------------------------------------------------------------------


class Committed:
    """Sessions sur des connexions distinctes, avec de vrais commits (concurrence réelle) ;
    ce qui est créé est supprimé en fin de test."""

    def __init__(self, engine: Engine) -> None:
        self.factory = make_session_factory(engine)
        self.runs: list[uuid.UUID] = []

    def refund(self, ticket: Ticket, payment: Payment) -> uuid.UUID:
        now = datetime.now(UTC)
        with self.factory() as session:
            agent_run = Run(
                agent=AgentKind.SUPPORT,
                ticket_id=ticket.id,
                status=RunStatus.SUCCEEDED,
                output=DECISION,
                created_at=now,
            )
            session.add(agent_run)
            session.flush()
            self.runs.append(agent_run.id)
            pending = PendingAction(
                run_id=agent_run.id,
                ticket_id=ticket.id,
                tool=REFUND,
                params={
                    "ticket_id": ticket.id,
                    "order_id": payment.order_id,
                    "amount_cents": payment.amount_cents,
                    "reason": "Retour",
                },
                reasons=["over_threshold"],
                facts={"value_cents": payment.amount_cents},
                idempotency_key=idempotency.refund_key(ticket.id, payment.order_id),
                status=PendingStatus.PENDING,
                created_at=now,
            )
            session.add(pending)
            session.commit()
            return pending.id

    def cleanup(self) -> None:
        with self.factory() as session:
            session.execute(delete(PendingAction).where(PendingAction.run_id.in_(self.runs)))
            session.execute(delete(Run).where(Run.id.in_(self.runs)))
            session.commit()


@pytest.fixture
def committed(agentpilot_engine: Engine) -> Iterator[Committed]:
    committed = Committed(agentpilot_engine)
    yield committed
    committed.cleanup()


def test_simultaneous_claims_have_one_winner(committed: Committed, shop: Shop) -> None:
    payment = shop.payment(12_000)
    action_id = committed.refund(shop.ticket(payment), payment)
    start = threading.Barrier(8)
    won: list[bool] = []

    def claim(n: int) -> None:
        start.wait()
        won.append(
            decisions._claim(
                committed.factory, action_id, PendingStatus.APPROVED, f"humain {n}", None
            )
        )

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(won) == [False] * 7 + [True]


@pytest.mark.anyio
async def test_double_approval_executes_once(
    committed: Committed, shop: Shop, shopsim: str
) -> None:
    payment = shop.payment(12_000)
    action_id = committed.refund(shop.ticket(payment), payment)

    outcomes = await asyncio.gather(
        decisions.approve(committed.factory, action_id, "Alice"),
        decisions.approve(committed.factory, action_id, "Bob"),
    )

    assert sorted(outcomes) == sorted([Notice.EXECUTED, Notice.ALREADY_DECIDED])
    assert shop.refunds(payment) == 1
    done = decisions.load(committed.factory, action_id)
    assert done is not None and done.status is PendingStatus.EXECUTED


# --- Action devenue impossible -----------------------------------------------------------


def test_ticket_resolved_in_the_meantime(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    ticket, payment, action_id = held_refund(client, shop)
    ticket.status = TicketStatus.RESOLVED
    session.flush()

    response = post(
        client,
        f"/approvals/{action_id}/approve",
        **ALICE,
        message="C'est fait.",
        send_message="true",
        resolve="true",
    )

    assert notices(response) == [Notice.IMPOSSIBLE, Notice.MESSAGE_SKIPPED]
    done = action(session_factory, action_id)
    assert done.status is PendingStatus.FAILED
    assert "résolu entre-temps" in (done.error or "")
    assert shop.refunds(payment) == 0
    assert decision_messages(session, ticket) == []


def test_payment_refunded_in_the_meantime(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    _, payment, action_id = held_refund(client, shop)
    session.add(
        Refund(
            payment=payment,
            amount_cents=5_000,
            reason="Geste commercial",
            idempotency_key=f"test-approvals-{payment.id}",
            created_at=datetime(2026, 7, 6, tzinfo=UTC),
        )
    )
    session.flush()

    response = post(client, f"/approvals/{action_id}/approve", **ALICE)

    assert notices(response) == [Notice.IMPOSSIBLE]
    done = action(session_factory, action_id)
    assert done.status is PendingStatus.FAILED
    assert "Il ne reste que 70,00 €" in (done.error or "")
    assert shop.refunds(payment) == 1


def test_resend_refused_by_shopsim(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    payment = shop.payment(15_000)
    ticket = shop.ticket(payment)
    step = call("resend_order", ticket_id=ticket.id, order_id=payment.order_id, reason="Cassé")
    [pending] = run(client, ticket, step)["pending_actions"]
    # Livraison qui n'est plus renvoyable : ShopSim refuse (409)
    payment.order.shipment.status = ShipmentStatus.IN_TRANSIT
    session.flush()

    response = post(client, f"/approvals/{pending['id']}/approve", **ALICE)

    assert notices(response) == [Notice.IMPOSSIBLE]
    done = action(session_factory, pending["id"])
    assert done.status is PendingStatus.FAILED
    assert "409" in (done.error or "")
    assert session.scalar(select(func.count()).select_from(Resend)) == 0


# --- ShopSim en panne pendant l'exécution ------------------------------------------------


def test_unavailable_shopsim_keeps_the_action_approved_then_retry(
    client: TestClient,
    shop: Shop,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, payment, action_id = held_refund(client, shop)
    real_post = decisions.post_json

    async def lost_response(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        # Le remboursement a lieu, mais la réponse se perd
        await real_post(*args, **kwargs)
        raise ShopSimUnavailable("ShopSim n'a pas répondu en 10 s.")

    monkeypatch.setattr(decisions, "post_json", lost_response)
    first = post(client, f"/approvals/{action_id}/approve", **ALICE)
    assert notices(first) == [Notice.UNAVAILABLE]
    pending = action(session_factory, action_id)
    assert pending.status is PendingStatus.APPROVED
    assert "n'a pas répondu" in (pending.error or "")

    # Relance : même clé, ShopSim reconnaît le rejeu malgré le reste remboursable à zéro
    monkeypatch.setattr(decisions, "post_json", real_post)
    retried = post(client, f"/approvals/{action_id}/retry")

    assert notices(retried) == [Notice.EXECUTED]
    assert action(session_factory, action_id).status is PendingStatus.EXECUTED
    assert shop.refunds(payment) == 1


def test_only_an_approved_action_can_be_retried(client: TestClient, shop: Shop) -> None:
    _, payment, action_id = held_refund(client, shop)

    assert notices(post(client, f"/approvals/{action_id}/retry")) == [Notice.NOT_RETRYABLE]
    assert shop.refunds(payment) == 0


# --- Message et résolution ---------------------------------------------------------------


def test_approval_with_message_and_resolution(
    client: TestClient, shop: Shop, session: Session, session_factory: sessionmaker[Session]
) -> None:
    ticket, _, action_id = held_refund(client, shop)
    message = "Bonjour,\n\nVotre remboursement est fait.\n\nLe service client Lumen"

    response = post(
        client,
        f"/approvals/{action_id}/approve",
        **ALICE,
        message=message,
        send_message="true",
        resolve="true",
    )

    assert notices(response) == [Notice.EXECUTED, Notice.MESSAGE_SENT, Notice.RESOLVED]
    session.refresh(ticket)
    assert ticket.status is TicketStatus.RESOLVED
    [sent] = decision_messages(session, ticket)
    assert sent.body == message and sent.author is MessageAuthor.HUMAN
    done = action(session_factory, action_id)
    assert done.customer_message == message
    assert done.message_sent_at is not None and done.resolved_at is not None

    again = post(client, f"/approvals/{action_id}/follow-up", message=message, send_message="true")
    assert notices(again) == [Notice.MESSAGE_ALREADY_SENT]
    assert len(decision_messages(session, ticket)) == 1


def test_browser_line_breaks_are_normalized(
    client: TestClient, shop: Shop, session: Session
) -> None:
    ticket, _, action_id = held_refund(client, shop)

    post(
        client,
        f"/approvals/{action_id}/approve",
        **ALICE,
        message="Bonjour,\r\n\r\nC'est fait.\r\n",
        send_message="true",
    )

    [sent] = decision_messages(session, ticket)
    assert sent.body == "Bonjour,\n\nC'est fait."


def test_resolution_waits_for_the_other_actions_of_the_ticket(
    client: TestClient, shop: Shop, session: Session
) -> None:
    first = shop.payment(12_000)
    second = shop.payment(9_000, first.order.customer)
    ticket = shop.ticket(first)
    result = run(client, ticket, refund(ticket, first, 12_000), refund(ticket, second, 9_000))
    action_id = result["pending_actions"][0]["id"]

    response = post(client, f"/approvals/{action_id}/approve", **ALICE, resolve="true")

    assert notices(response) == [Notice.EXECUTED, Notice.RESOLVE_BLOCKED]
    session.refresh(ticket)
    assert ticket.status is TicketStatus.OPEN


def test_follow_up_needs_a_decision(client: TestClient, shop: Shop) -> None:
    _, _, action_id = held_refund(client, shop)

    response = post(
        client, f"/approvals/{action_id}/follow-up", message="Bonjour", send_message="true"
    )

    assert response.status_code == 422


# --- Écart de montant --------------------------------------------------------------------


def test_understated_amount_warning_on_top_of_the_page(client: TestClient, shop: Shop) -> None:
    _, _, action_id = held_refund(client, shop, amount=12_000, requested=3_000)

    page = client.get(f"/approvals/{action_id}").text

    warning = "Montant demandé : 30,00 € — reste dû : 120,00 €"
    assert warning in page
    # En haut : avant le dossier
    assert page.index(warning) < page.index("Action demandée par l'agent")


def test_no_warning_when_amounts_match(client: TestClient, shop: Shop) -> None:
    _, _, action_id = held_refund(client, shop)

    assert "reste dû" not in client.get(f"/approvals/{action_id}").text


@pytest.mark.parametrize(
    ("declared", "warning"),
    [
        (600, "Valeur annoncée par l'agent : 6,00 € — valeur de la commande : 6,41 €"),
        (641, None),
        (None, None),
    ],
)
def test_resend_mismatch(declared: int | None, warning: str | None) -> None:
    resend = PendingAction(tool=RESEND, params={"order_id": 42}, facts={"order_total_cents": 641})

    assert decisions.amount_mismatch(resend, {"amount_cents": declared}) == warning


def test_eur() -> None:
    assert decisions.eur(3_850_900) == "38 509,00 €"
    assert decisions.eur(5) == "0,05 €"


# --- Pages -------------------------------------------------------------------------------


def test_list_and_detail_pages(client: TestClient, shop: Shop) -> None:
    ticket, _, action_id = held_refund(client, shop)

    listing = client.get("/approvals")
    detail = client.get(f"/approvals/{action_id}")

    assert listing.status_code == 200 and f"#{ticket.id}" in listing.text
    assert detail.status_code == 200
    for expected in ["120,00 €", "montant supérieur au seuil", "Approuver", "Refuser"]:
        assert expected in detail.text
    assert client.get("/approvals?status=executed").text.count(f"#{ticket.id}<") == 0


def test_ticket_content_is_escaped(client: TestClient, shop: Shop, session: Session) -> None:
    ticket, _, action_id = held_refund(client, shop)
    ticket.messages.append(
        TicketMessage(
            author=MessageAuthor.CUSTOMER,
            body="<script>alert('x')</script>",
            created_at=datetime(2026, 7, 6, tzinfo=UTC),
        )
    )
    session.flush()

    page = client.get(f"/approvals/{action_id}").text

    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


def test_unknown_action_is_404(client: TestClient) -> None:
    assert client.get(f"/approvals/{uuid.uuid4()}").status_code == 404
