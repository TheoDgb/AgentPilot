from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim.app.models import MessageAuthor, OrderStatus, Ticket, TicketStatus
from shopsim.tests.conftest import MakeCustomer, MakeOrder, MakeTicket

T0 = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)


def test_get_ticket_with_messages_in_chronological_order(
    client: TestClient, session: Session, make_order: MakeOrder, make_ticket: MakeTicket
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    # Insérés volontairement dans le désordre
    ticket = make_ticket(
        order.customer,
        order,
        status=TicketStatus.PENDING,
        created_at=T0,
        messages=[
            (MessageAuthor.HUMAN, "Pouvez-vous envoyer une photo ?", T0 + timedelta(hours=5)),
            (MessageAuthor.CUSTOMER, "Mon colis est abîmé.", T0),
            (MessageAuthor.AGENT, "Nous regardons.", T0 + timedelta(hours=1)),
        ],
    )
    session.expire_all()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == ticket.id
    assert body["customer_id"] == order.customer_id
    assert body["order_id"] == order.id
    assert body["subject"] == "Question"
    assert body["status"] == "pending"
    assert datetime.fromisoformat(body["created_at"]) == T0
    assert [(m["author"], m["body"]) for m in body["messages"]] == [
        ("customer", "Mon colis est abîmé."),
        ("agent", "Nous regardons."),
        ("human", "Pouvez-vous envoyer une photo ?"),
    ]


def test_get_ticket_without_order(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer())

    assert client.get(f"/tickets/{ticket.id}").json()["order_id"] is None


def test_get_missing_ticket_returns_404(client: TestClient) -> None:
    response = client.get("/tickets/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found"}


def test_list_tickets_filters_by_status(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    customer = make_customer()
    open_ids = [make_ticket(customer, created_at=T0 + timedelta(hours=i)).id for i in range(3)]
    make_ticket(customer, status=TicketStatus.RESOLVED)
    make_ticket(customer, status=TicketStatus.ESCALATED)

    body = client.get("/tickets", params={"status": "open"}).json()

    assert body["total"] == 3
    assert [t["id"] for t in body["items"]] == open_ids
    assert all(t["status"] == "open" for t in body["items"])
    assert "messages" not in body["items"][0]
    assert client.get("/tickets").json()["total"] == 5


def test_list_tickets_pagination(
    client: TestClient, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    customer = make_customer()
    # Deux tickets à la même date : l'id départage
    times = [T0 + timedelta(hours=h) for h in (3, 0, 1, 1, 2, 5, 4)]
    tickets = [make_ticket(customer, created_at=at) for at in times]
    expected = [t.id for t in sorted(tickets, key=lambda t: (t.created_at, t.id))]

    pages = [
        client.get("/tickets", params={"limit": 3, "offset": offset}).json() for offset in (0, 3, 6)
    ]

    assert [p["total"] for p in pages] == [7, 7, 7]
    assert [p["offset"] for p in pages] == [0, 3, 6]
    assert [p["limit"] for p in pages] == [3, 3, 3]
    assert [len(p["items"]) for p in pages] == [3, 3, 1]
    # Ni doublon ni trou, dans l'ordre stable
    assert [t["id"] for p in pages for t in p["items"]] == expected
    beyond = client.get("/tickets", params={"limit": 3, "offset": 9}).json()
    assert beyond["items"] == []
    assert beyond["total"] == 7


def test_list_tickets_default_page(
    client: TestClient, session: Session, make_customer: MakeCustomer
) -> None:
    customer = make_customer()
    session.add_all(
        Ticket(customer=customer, subject="Q", status=TicketStatus.OPEN, created_at=T0)
        for _ in range(25)
    )
    session.flush()

    body = client.get("/tickets").json()

    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 20
    assert body["total"] == 25


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "closed"}],
)
def test_list_tickets_rejects_invalid_parameters(
    client: TestClient, params: dict[str, str | int]
) -> None:
    assert client.get("/tickets", params=params).status_code == 422
