"""Actions sur les tickets : réponse idempotente, escalade, résolution."""

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from shopsim.app.models import Ticket, TicketStatus
from shopsim.tests.conftest import MakeCustomer, MakeTicket

REASON = "Remboursement au-dessus du seuil"


def post_reply(
    client: TestClient,
    ticket_id: int,
    body: str = "Bonjour, voici la réponse.",
    key: str | None = "k",
) -> Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/tickets/{ticket_id}/reply", json={"body": body}, headers=headers)


def escalate(client: TestClient, ticket_id: int, reason: str = REASON) -> Response:
    return client.post(f"/tickets/{ticket_id}/escalate", json={"reason": reason})


@pytest.fixture
def ticket(make_customer: MakeCustomer, make_ticket: MakeTicket) -> Ticket:
    return make_ticket(make_customer())


# --- Réponse ---------------------------------------------------------------------------


def test_reply_adds_agent_message(client: TestClient, ticket: Ticket) -> None:
    response = post_reply(client, ticket.id, key="reply-1")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "open"
    assert [m["author"] for m in body["messages"]] == ["customer", "agent"]
    assert body["messages"][-1]["body"] == "Bonjour, voici la réponse."
    assert client.get(f"/tickets/{ticket.id}").json()["messages"] == body["messages"]


def test_replay_same_key_creates_a_single_message(client: TestClient, ticket: Ticket) -> None:
    first = post_reply(client, ticket.id, key="reply-replay")
    second = post_reply(client, ticket.id, key="reply-replay")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(second.json()["messages"]) == 2


def test_same_key_with_different_body_is_rejected(client: TestClient, ticket: Ticket) -> None:
    assert post_reply(client, ticket.id, "Première", key="reply-conflict").status_code == 201

    response = post_reply(client, ticket.id, "Autre texte", key="reply-conflict")

    assert response.status_code == 409
    assert response.json() == {"detail": "Idempotency-Key already used with a different request"}
    assert len(client.get(f"/tickets/{ticket.id}").json()["messages"]) == 2


def test_same_key_on_another_ticket_is_rejected(
    client: TestClient, ticket: Ticket, make_ticket: MakeTicket
) -> None:
    other = make_ticket(ticket.customer)
    assert post_reply(client, ticket.id, key="reply-shared").status_code == 201

    response = post_reply(client, other.id, key="reply-shared")

    assert response.status_code == 409
    assert len(client.get(f"/tickets/{other.id}").json()["messages"]) == 1


@pytest.mark.parametrize("key", [None, "", "   "])
def test_reply_without_idempotency_key_is_rejected(
    client: TestClient, ticket: Ticket, key: str | None
) -> None:
    response = post_reply(client, ticket.id, key=key)

    assert response.status_code == 400
    assert response.json() == {"detail": "Idempotency-Key header is required"}


def test_reply_to_missing_ticket_returns_404(client: TestClient) -> None:
    response = post_reply(client, 999999, key="reply-missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


def test_reply_to_resolved_ticket_is_rejected(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer(), status=TicketStatus.RESOLVED)

    response = post_reply(client, ticket.id, key="reply-resolved")

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket is resolved"}


def test_replay_after_resolution_still_succeeds(client: TestClient, ticket: Ticket) -> None:
    first = post_reply(client, ticket.id, key="reply-then-resolve")
    client.post(f"/tickets/{ticket.id}/resolve")

    replay = post_reply(client, ticket.id, key="reply-then-resolve")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["status"] == "resolved"
    assert len(replay.json()["messages"]) == 2


@pytest.mark.parametrize("payload", [{"body": ""}, {"body": "ok", "author": "human"}, {}])
def test_reply_with_invalid_body_is_rejected(
    client: TestClient, ticket: Ticket, payload: dict[str, str]
) -> None:
    response = client.post(
        f"/tickets/{ticket.id}/reply", json=payload, headers={"Idempotency-Key": "reply-422"}
    )

    assert response.status_code == 422


# --- Escalade --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [TicketStatus.OPEN, TicketStatus.PENDING])
def test_escalate_sets_status_and_reason(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket, status: TicketStatus
) -> None:
    ticket = make_ticket(make_customer(), status=status)

    response = escalate(client, ticket.id)

    assert response.status_code == 200
    assert response.json()["status"] == "escalated"
    assert response.json()["escalation_reason"] == REASON
    assert client.get(f"/tickets/{ticket.id}").json()["escalation_reason"] == REASON


def test_escalate_again_with_same_reason_is_a_no_op(client: TestClient, ticket: Ticket) -> None:
    first = escalate(client, ticket.id)

    again = escalate(client, ticket.id)

    assert again.status_code == 200
    assert again.json() == first.json()


def test_escalate_again_with_other_reason_is_rejected(client: TestClient, ticket: Ticket) -> None:
    escalate(client, ticket.id)

    response = escalate(client, ticket.id, "Autre motif")

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket already escalated with a different reason"}
    assert client.get(f"/tickets/{ticket.id}").json()["escalation_reason"] == REASON


def test_escalate_resolved_ticket_is_rejected(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer(), status=TicketStatus.RESOLVED)

    response = escalate(client, ticket.id)

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket is resolved"}
    assert client.get(f"/tickets/{ticket.id}").json()["escalation_reason"] is None


def test_escalate_missing_ticket_returns_404(client: TestClient) -> None:
    assert escalate(client, 999999).status_code == 404


@pytest.mark.parametrize("payload", [{"reason": ""}, {}, {"reason": "x", "status": "open"}])
def test_escalate_with_invalid_body_is_rejected(
    client: TestClient, ticket: Ticket, payload: dict[str, str]
) -> None:
    assert client.post(f"/tickets/{ticket.id}/escalate", json=payload).status_code == 422


# --- Résolution ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [TicketStatus.OPEN, TicketStatus.PENDING, TicketStatus.ESCALATED]
)
def test_resolve_sets_status(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket, status: TicketStatus
) -> None:
    ticket = make_ticket(make_customer(), status=status)

    response = client.post(f"/tickets/{ticket.id}/resolve")

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


def test_resolve_keeps_escalation_reason(client: TestClient, ticket: Ticket) -> None:
    escalate(client, ticket.id)

    response = client.post(f"/tickets/{ticket.id}/resolve")

    assert response.json()["status"] == "resolved"
    assert response.json()["escalation_reason"] == REASON


def test_resolve_twice_is_a_no_op(client: TestClient, ticket: Ticket) -> None:
    post_reply(client, ticket.id, key="reply-before-resolve")
    first = client.post(f"/tickets/{ticket.id}/resolve")

    again = client.post(f"/tickets/{ticket.id}/resolve")

    assert again.status_code == 200
    assert again.json() == first.json()


def test_resolve_missing_ticket_returns_404(client: TestClient) -> None:
    response = client.post("/tickets/999999/resolve")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


def test_resolve_counts_in_customer_profile(client: TestClient, ticket: Ticket) -> None:
    before = client.get(f"/customers/{ticket.customer_id}").json()["resolved_tickets_count"]

    client.post(f"/tickets/{ticket.id}/resolve")
    client.post(f"/tickets/{ticket.id}/resolve")

    after = client.get(f"/customers/{ticket.customer_id}").json()["resolved_tickets_count"]
    assert after == before + 1
