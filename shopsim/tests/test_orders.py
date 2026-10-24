from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim.app.models import Customer, Order, OrderStatus


def test_get_existing_order(client: TestClient, session: Session) -> None:
    created = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    customer = Customer(
        email="test@example.com", first_name="Test", last_name="Client", created_at=created
    )
    order = Order(
        customer=customer,
        status=OrderStatus.PAID,
        total_cents=4990,
        currency="EUR",
        created_at=created,
    )
    session.add(order)
    session.flush()

    response = client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == order.id
    assert body["customer_id"] == customer.id
    assert body["status"] == "paid"
    assert body["total_cents"] == 4990
    assert body["currency"] == "EUR"
    assert datetime.fromisoformat(body["created_at"]) == created


def test_get_missing_order_returns_404(client: TestClient) -> None:
    response = client.get("/orders/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}
