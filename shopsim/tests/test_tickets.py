from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.models import MessageAuthor, Order, OrderStatus, Ticket, TicketStatus
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


# --- Création par un client (POST /tickets, J4.3) ---------------------------------------


def create(
    client: TestClient, payload: dict[str, object], key: str | None = "create-1"
) -> Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/tickets", json=payload, headers=headers)


def payload_for(order: Order, **changes: object) -> dict[str, object]:
    return {
        "customer_id": order.customer_id,
        "order_id": order.id,
        "subject": f"Question sur ma commande {order.id}",
        "body": "Bonjour, où en est ma commande ?",
    } | changes


def test_create_ticket_opens_it_with_the_customer_message(
    client: TestClient, make_order: MakeOrder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOPSIM_FREEZE_CLOCK", "1")
    order = make_order(OrderStatus.SHIPPED)

    response = create(client, payload_for(order))

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "open"
    assert (ticket["customer_id"], ticket["order_id"]) == (order.customer_id, order.id)
    assert ticket["subject"] == f"Question sur ma commande {order.id}"
    assert ticket["created_at"] == "2026-07-06T12:00:00Z"
    assert ticket["messages"] == [
        {
            "author": "customer",
            "body": "Bonjour, où en est ma commande ?",
            "created_at": "2026-07-06T12:00:00Z",
        }
    ]
    page = client.get("/tickets", params={"status": "open", "limit": 100}).json()
    assert ticket["id"] in [t["id"] for t in page["items"]]


def test_create_ticket_without_order(client: TestClient, make_customer: MakeCustomer) -> None:
    customer = make_customer()

    response = create(
        client, {"customer_id": customer.id, "subject": "Transporteurs", "body": "Lesquels ?"}
    )

    assert response.status_code == 201
    assert response.json()["order_id"] is None


def test_create_ticket_replay_returns_the_same_ticket(
    client: TestClient, make_order: MakeOrder, session: Session
) -> None:
    order = make_order()

    first = create(client, payload_for(order), key="create-replay")
    again = create(client, payload_for(order), key="create-replay")

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    count = session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.customer_id == order.customer_id)
    )
    assert count == 1


def test_create_ticket_key_reused_with_other_content_is_409(
    client: TestClient, make_order: MakeOrder
) -> None:
    order = make_order()
    create(client, payload_for(order), key="create-conflict")

    response = create(client, payload_for(order, body="Autre texte"), key="create-conflict")

    assert response.status_code == 409


def test_create_ticket_key_of_a_reply_is_409(
    client: TestClient, make_order: MakeOrder, make_ticket: MakeTicket
) -> None:
    order = make_order()
    ticket = make_ticket(order.customer, order)
    client.post(
        f"/tickets/{ticket.id}/reply", json={"body": "Réponse"}, headers={"Idempotency-Key": "k-r"}
    )

    assert create(client, payload_for(order, body="Réponse"), key="k-r").status_code == 409


def test_create_ticket_requires_a_key(client: TestClient, make_order: MakeOrder) -> None:
    assert create(client, payload_for(make_order()), key=None).status_code == 400


def test_create_ticket_unknown_customer_or_order_is_404(
    client: TestClient, make_order: MakeOrder
) -> None:
    order = make_order()

    unknown_customer = create(client, payload_for(order, customer_id=999_999), key="c-404")
    unknown_order = create(client, payload_for(order, order_id=999_999), key="o-404")

    assert unknown_customer.status_code == 404
    assert unknown_customer.json()["detail"] == "Customer not found"
    assert unknown_order.status_code == 404
    assert unknown_order.json()["detail"] == "Order not found"


@pytest.mark.parametrize(
    "changes",
    [{"subject": ""}, {"body": ""}, {"subject": "x" * 201}, {"status": "resolved"}],
)
def test_create_ticket_validation(
    client: TestClient, make_order: MakeOrder, changes: dict[str, object]
) -> None:
    assert create(client, payload_for(make_order(), **changes)).status_code == 422
