"""Fixtures : ShopSim de test (client de test, base `shopsim_test`) et petite boutique cohérente.

Les ids de la base de test ne partent pas de 1 (séquences partagées entre les tests) : le
catalogue est construit à partir des objets créés, pas par la découverte (testée à part).
"""

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim.app.models import (
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    ShipmentStatus,
)
from shopsim.tests import conftest as shopsim_fixtures
from traffic.client import Send
from traffic.visits import Catalog, OrderRef

db_engine = shopsim_fixtures.db_engine
session = shopsim_fixtures.session
client = shopsim_fixtures.client
no_payment_latency = shopsim_fixtures.no_payment_latency
make_customer = shopsim_fixtures.make_customer
make_order = shopsim_fixtures.make_order
make_shipment = shopsim_fixtures.make_shipment
make_ticket = shopsim_fixtures.make_ticket

MakeCustomer = shopsim_fixtures.MakeCustomer
MakeOrder = shopsim_fixtures.MakeOrder
MakeShipment = shopsim_fixtures.MakeShipment

_amounts = itertools.count(4_000, 1_317)

# État de commande → (statut de commande, statut de paiement, statut de livraison)
STATES: dict[str, tuple[OrderStatus, PaymentStatus, ShipmentStatus | None]] = {
    "pending": (OrderStatus.PENDING, PaymentStatus.PENDING, None),
    "cancelled": (OrderStatus.CANCELLED, PaymentStatus.FAILED, None),
    "paid": (OrderStatus.PAID, PaymentStatus.SUCCEEDED, None),
    "moving": (OrderStatus.SHIPPED, PaymentStatus.SUCCEEDED, ShipmentStatus.IN_TRANSIT),
    "delayed": (OrderStatus.SHIPPED, PaymentStatus.SUCCEEDED, ShipmentStatus.DELAYED),
    "lost": (OrderStatus.SHIPPED, PaymentStatus.SUCCEEDED, ShipmentStatus.LOST),
    "delivered": (OrderStatus.DELIVERED, PaymentStatus.SUCCEEDED, ShipmentStatus.DELIVERED),
}


@dataclass
class Shop:
    catalog: Catalog
    # Commande → état de `STATES`
    states: dict[int, str]
    orders: dict[int, Order]


@pytest.fixture
def shop(
    session: Session,
    make_customer: MakeCustomer,
    make_order: MakeOrder,
    make_shipment: MakeShipment,
) -> Shop:
    """3 clients ; chacun a une commande dans chaque état, avec paiement, et livraison si
    expédiée : tout ce qu'un parcours demande existe."""
    customers: list[Customer] = [make_customer() for _ in range(3)]
    states: dict[int, str] = {}
    orders: dict[int, Order] = {}
    for customer in customers:
        for state, (order_status, payment_status, shipment_status) in STATES.items():
            amount = next(_amounts)
            order = make_order(order_status, amount, customer=customer)
            session.add(
                Payment(
                    order=order,
                    status=payment_status,
                    amount_cents=amount,
                    created_at=order.created_at,
                )
            )
            if shipment_status is not None:
                delivered = (
                    datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
                    if shipment_status is ShipmentStatus.DELIVERED
                    else None
                )
                make_shipment(order, shipment_status, delivered)
            states[order.id] = state
            orders[order.id] = order
    session.flush()
    catalog = Catalog(
        customers=[c.id for c in customers],
        orders={
            c.id: [OrderRef(o.id, c.id, o.status.value) for o in orders.values() if o.customer is c]
            for c in customers
        },
    )
    return Shop(catalog, states, orders)


@pytest.fixture
def send(client: TestClient) -> Send:
    """Envoi par le client de test de ShopSim (même application, même base)."""

    def _send(method: str, path: str, payload: Any, headers: Mapping[str, str]) -> tuple[int, Any]:  # noqa: ANN401
        response = client.request(method, path, json=payload, headers=dict(headers))
        return response.status_code, response.json() if response.content else None

    return _send
