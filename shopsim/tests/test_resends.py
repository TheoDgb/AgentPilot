"""Renvoi idempotent d'un article : commande et livraison de remplacement."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.models import Order, OrderStatus, Resend, ShipmentStatus
from shopsim.tests.conftest import MakeOrder, MakeShipment, SimClock


def resend(
    client: TestClient, order_id: int, key: str | None, reason: str = "Défectueux"
) -> Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/orders/{order_id}/resend", json={"reason": reason}, headers=headers)


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Resend)) or 0


@pytest.fixture
def delivered(make_order: MakeOrder, make_shipment: MakeShipment) -> Order:
    order = make_order(OrderStatus.DELIVERED, total_cents=4_990)
    make_shipment(order, ShipmentStatus.DELIVERED, datetime(2026, 7, 1, 15, 0, tzinfo=UTC))
    return order


def test_resend_creates_replacement_order_and_shipment(
    client: TestClient, session: Session, delivered: Order, sim_clock: SimClock
) -> None:
    response = resend(client, delivered.id, "resend-1")

    assert response.status_code == 201
    body = response.json()
    assert body["order_id"] == delivered.id
    assert body["reason"] == "Défectueux"
    assert body["carrier"] == "Rapidis"
    assert body["tracking_number"].startswith("RPD")
    assert body["tracking_number"] != delivered.shipment.tracking_number  # type: ignore[union-attr]

    replacement = client.get(f"/orders/{body['replacement_order_id']}").json()
    assert replacement["customer_id"] == delivered.customer_id
    assert replacement["status"] == "shipped"
    # Gratuit pour le client : aucun paiement
    assert replacement["total_cents"] == 0
    assert client.get(f"/payments/{body['replacement_order_id']}").status_code == 404

    shipment = client.get(f"/shipments/{body['replacement_order_id']}").json()
    assert shipment["status"] == "label_created"
    assert shipment["tracking_number"] == body["tracking_number"]
    assert shipment["estimated_delivery"] == "2026-07-09"
    assert [e["description"] for e in shipment["events"]] == ["Étiquette créée"]
    # La livraison d'origine ne change pas
    assert client.get(f"/shipments/{delivered.id}").json()["status"] == "delivered"


def test_lost_parcel_can_be_resent(
    client: TestClient, make_order: MakeOrder, make_shipment: MakeShipment
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    make_shipment(order, ShipmentStatus.LOST)

    assert resend(client, order.id, "resend-lost", "Colis perdu").status_code == 201


def test_replay_returns_the_same_resend(
    client: TestClient, session: Session, delivered: Order
) -> None:
    first = resend(client, delivered.id, "resend-replay")

    again = resend(client, delivered.id, "resend-replay")

    assert again.status_code == 200
    assert again.json() == first.json()
    assert count(session) == 1


def test_same_key_with_other_reason_is_rejected(
    client: TestClient, session: Session, delivered: Order
) -> None:
    resend(client, delivered.id, "resend-conflict")

    response = resend(client, delivered.id, "resend-conflict", "Autre motif")

    assert response.status_code == 409
    assert response.json() == {"detail": "Idempotency-Key already used with a different request"}
    assert count(session) == 1


def test_order_is_resent_only_once(client: TestClient, session: Session, delivered: Order) -> None:
    resend(client, delivered.id, "resend-a")

    response = resend(client, delivered.id, "resend-b")

    assert response.status_code == 409
    assert response.json() == {"detail": "Order already resent"}
    assert count(session) == 1


@pytest.mark.parametrize("status", [ShipmentStatus.IN_TRANSIT, ShipmentStatus.DELAYED])
def test_parcel_still_on_its_way_cannot_be_resent(
    client: TestClient,
    session: Session,
    make_order: MakeOrder,
    make_shipment: MakeShipment,
    status: ShipmentStatus,
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    make_shipment(order, status)

    response = resend(client, order.id, "resend-moving")

    assert response.status_code == 409
    assert response.json() == {"detail": "Order cannot be resent"}
    assert count(session) == 0


def test_order_without_shipment_cannot_be_resent(client: TestClient, make_order: MakeOrder) -> None:
    order = make_order(OrderStatus.PAID)

    assert resend(client, order.id, "resend-unshipped").status_code == 409


def test_missing_key_is_rejected(client: TestClient, session: Session, delivered: Order) -> None:
    response = resend(client, delivered.id, None)

    assert response.status_code == 400
    assert response.json() == {"detail": "Idempotency-Key header is required"}
    assert count(session) == 0


def test_missing_order_returns_404(client: TestClient) -> None:
    response = resend(client, 999_999, "resend-missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}


@pytest.mark.parametrize(
    "payload", [{}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "ok", "x": 1}]
)
def test_invalid_body_is_rejected(
    client: TestClient, delivered: Order, payload: dict[str, object]
) -> None:
    response = client.post(
        f"/orders/{delivered.id}/resend", json=payload, headers={"Idempotency-Key": "resend-bad"}
    )

    assert response.status_code == 422
