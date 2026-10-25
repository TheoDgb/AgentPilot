from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim.app.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    TicketStatus,
)
from shopsim.tests.conftest import MakeCustomer, MakeOrder, MakeTicket


def _refund(session: Session, order: Order, amount_cents: int, key: str) -> None:
    payment = Payment(
        order=order,
        status=PaymentStatus.SUCCEEDED,
        amount_cents=order.total_cents,
        created_at=order.created_at,
    )
    session.add(
        Refund(
            payment=payment,
            amount_cents=amount_cents,
            reason="Geste commercial",
            idempotency_key=key,
            created_at=order.created_at,
        )
    )
    session.flush()


def test_get_customer_profile_with_counters(
    client: TestClient,
    session: Session,
    make_customer: MakeCustomer,
    make_order: MakeOrder,
    make_ticket: MakeTicket,
) -> None:
    customer = make_customer(created_at=datetime(2026, 1, 6, 12, 0, tzinfo=UTC))
    first = make_order(OrderStatus.DELIVERED, customer=customer)
    second = make_order(OrderStatus.DELIVERED, customer=customer)
    _refund(session, first, 1000, "test-customer-refund-1")
    _refund(session, second, 2000, "test-customer-refund-2")
    for status in TicketStatus:
        make_ticket(customer, status=status)
    make_ticket(customer, status=TicketStatus.RESOLVED)

    # Données d'un autre client : jamais comptées
    other = make_order(OrderStatus.DELIVERED)
    _refund(session, other, 500, "test-customer-refund-other")
    make_ticket(other.customer, status=TicketStatus.RESOLVED)

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    body = response.json()
    assert datetime.fromisoformat(body.pop("created_at")) == customer.created_at
    assert body == {
        "id": customer.id,
        "email": customer.email,
        "first_name": "Test",
        "last_name": "Client",
        # Du 6 janvier au 6 juillet 2026 à midi
        "tenure_days": 181,
        # Seulement les tickets resolved : open, pending et escalated sont exclus
        "resolved_tickets_count": 2,
        "refunds_count": 2,
    }


def test_get_customer_without_history(client: TestClient, make_customer: MakeCustomer) -> None:
    customer = make_customer()

    body = client.get(f"/customers/{customer.id}").json()

    assert body["resolved_tickets_count"] == 0
    assert body["refunds_count"] == 0


def test_get_missing_customer_returns_404(client: TestClient) -> None:
    response = client.get("/customers/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Customer not found"}


def test_list_customer_orders_most_recent_first(
    client: TestClient, make_customer: MakeCustomer, make_order: MakeOrder
) -> None:
    customer = make_customer()
    old = make_order(created_at=datetime(2026, 2, 1, tzinfo=UTC), customer=customer)
    recent = make_order(created_at=datetime(2026, 6, 1, tzinfo=UTC), customer=customer)
    middle = make_order(created_at=datetime(2026, 4, 1, tzinfo=UTC), customer=customer)
    make_order()  # commande d'un autre client

    response = client.get(f"/customers/{customer.id}/orders")

    assert response.status_code == 200
    body = response.json()
    assert [o["id"] for o in body] == [recent.id, middle.id, old.id]
    assert all(o["customer_id"] == customer.id for o in body)


def test_list_orders_of_customer_without_orders(
    client: TestClient, make_customer: MakeCustomer
) -> None:
    customer = make_customer()

    response = client.get(f"/customers/{customer.id}/orders")

    assert response.status_code == 200
    assert response.json() == []


def test_list_orders_of_missing_customer_returns_404(client: TestClient) -> None:
    response = client.get("/customers/999999/orders")

    assert response.status_code == 404
    assert response.json() == {"detail": "Customer not found"}
