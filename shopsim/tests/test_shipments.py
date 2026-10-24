from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim.app.models import OrderStatus, Shipment, ShipmentEvent, ShipmentStatus
from shopsim.tests.conftest import MakeOrder


def test_get_shipment_with_events_in_chronological_order(
    client: TestClient, session: Session, make_order: MakeOrder
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    shipment = Shipment(
        order=order,
        carrier="Rapidis",
        tracking_number="RPD0000000001",
        status=ShipmentStatus.IN_TRANSIT,
        estimated_delivery=date(2026, 7, 8),
        delivered_at=None,
        created_at=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
    )
    # Insérés volontairement dans le désordre
    shipment.events = [
        ShipmentEvent(
            occurred_at=datetime(2026, 7, 4, 6, 0, tzinfo=UTC),
            location="Plateforme de Lyon-Corbas",
            description="En transit — Plateforme de Lyon-Corbas",
        ),
        ShipmentEvent(
            occurred_at=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
            location="Entrepôt Lumen, Orléans",
            description="Étiquette créée",
        ),
        ShipmentEvent(
            occurred_at=datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
            location="Entrepôt Lumen, Orléans",
            description="Pris en charge par le transporteur",
        ),
    ]
    session.add(shipment)
    session.flush()
    session.expire_all()

    response = client.get(f"/shipments/{order.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == shipment.id
    assert body["order_id"] == order.id
    assert body["carrier"] == "Rapidis"
    assert body["tracking_number"] == "RPD0000000001"
    assert body["status"] == "in_transit"
    assert body["estimated_delivery"] == "2026-07-08"
    assert body["delivered_at"] is None
    assert [e["description"] for e in body["events"]] == [
        "Étiquette créée",
        "Pris en charge par le transporteur",
        "En transit — Plateforme de Lyon-Corbas",
    ]


def test_get_shipment_missing_returns_404(client: TestClient, make_order: MakeOrder) -> None:
    order = make_order(OrderStatus.PAID)

    response = client.get(f"/shipments/{order.id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Shipment not found"}
