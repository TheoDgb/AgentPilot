from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.clock import SIMULATION_NOW, simulation_today
from shopsim.app.models import Customer, Order, OrderStatus, Refund, Shipment, ShipmentStatus
from shopsim.seed import (
    N_CUSTOMERS,
    N_ORDERS,
    ORDER_AGE_BY_STATUS,
    PAYMENT_STATUS_BY_ORDER_STATUS,
    seed,
)

SHIPMENT_STATUSES_BY_ORDER_STATUS = {
    OrderStatus.SHIPPED: {
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELAYED,
        ShipmentStatus.LOST,
    },
    OrderStatus.DELIVERED: {ShipmentStatus.DELIVERED},
}


def test_insert_order_without_id_after_seed(session: Session) -> None:
    seed(session)
    assert session.scalar(select(func.count()).select_from(Customer)) == N_CUSTOMERS
    seeded_ids = set(session.scalars(select(Order.id)))
    assert len(seeded_ids) == N_ORDERS

    # Les séquences doivent suivre le seed : un INSERT sans id ne doit pas entrer en collision
    customer = session.scalars(select(Customer).limit(1)).one()
    order = Order(
        customer=customer,
        status=OrderStatus.PENDING,
        total_cents=1000,
        currency="EUR",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    session.add(order)
    session.flush()

    assert order.id not in seeded_ids


def test_seed_payments_match_order_status(session: Session) -> None:
    seed(session)

    orders = session.scalars(select(Order)).all()
    assert len(orders) == N_ORDERS
    for order in orders:
        assert order.payment is not None
        assert order.payment.amount_cents == order.total_cents
        assert order.payment.status == PAYMENT_STATUS_BY_ORDER_STATUS[order.status]
        expected_refunded = order.total_cents if order.status == OrderStatus.REFUNDED else 0
        assert order.payment.refunded_cents == expected_refunded


def test_seed_keeps_customers_statuses_and_amounts(session: Session) -> None:
    """Non-régression : redater les commandes (J1.3) n'a changé ni statuts ni montants."""
    seed(session)

    orders = session.scalars(select(Order)).all()
    assert Counter(o.status for o in orders) == {
        OrderStatus.PAID: 41,
        OrderStatus.CANCELLED: 38,
        OrderStatus.REFUNDED: 33,
        OrderStatus.DELIVERED: 32,
        OrderStatus.PENDING: 29,
        OrderStatus.SHIPPED: 27,
    }
    assert sum(o.total_cents for o in orders) == 5_070_492


def test_seed_order_dates_match_status(session: Session) -> None:
    seed(session)

    for order in session.scalars(select(Order)):
        min_age, max_age = ORDER_AGE_BY_STATUS[order.status]
        assert min_age <= SIMULATION_NOW - order.created_at <= max_age
        assert order.created_at >= order.customer.created_at
        assert order.created_at <= order.payment.created_at <= SIMULATION_NOW

    for refund in session.scalars(select(Refund)):
        assert refund.payment.created_at <= refund.created_at <= SIMULATION_NOW


def test_seed_delivered_orders_straddle_refund_window(session: Session) -> None:
    seed(session)

    shipments = session.scalars(
        select(Shipment).where(Shipment.status == ShipmentStatus.DELIVERED)
    ).all()
    ages = [SIMULATION_NOW - s.delivered_at for s in shipments if s.delivered_at is not None]
    assert any(age <= timedelta(days=30) for age in ages)
    assert any(age > timedelta(days=30) for age in ages)


def test_seed_shipments_match_order_status(session: Session) -> None:
    seed(session)

    for order in session.scalars(select(Order)):
        expected = SHIPMENT_STATUSES_BY_ORDER_STATUS.get(order.status)
        if expected is None:
            assert order.shipment is None
        else:
            assert order.shipment is not None
            assert order.shipment.status in expected


def test_seed_shipment_dates_are_consistent(session: Session) -> None:
    seed(session)
    today = simulation_today()

    shipments = session.scalars(select(Shipment)).all()
    assert len({s.tracking_number for s in shipments}) == len(shipments)
    for shipment in shipments:
        assert shipment.order.created_at <= shipment.created_at <= SIMULATION_NOW

        # Aucune livraison livrée sans delivered_at, et pas de delivered_at sinon
        if shipment.status == ShipmentStatus.DELIVERED:
            assert shipment.delivered_at is not None
            assert shipment.created_at < shipment.delivered_at <= SIMULATION_NOW
        else:
            assert shipment.delivered_at is None

        # Retards cohérents avec la date de référence
        if shipment.status in (ShipmentStatus.DELAYED, ShipmentStatus.LOST):
            assert shipment.estimated_delivery < today
        if shipment.status in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY):
            assert shipment.estimated_delivery >= today

        occurred = [event.occurred_at for event in shipment.events]
        assert occurred == sorted(occurred)
        assert occurred[0] == shipment.created_at
        assert occurred[-1] <= SIMULATION_NOW
        if shipment.status == ShipmentStatus.LOST:
            assert SIMULATION_NOW - occurred[-1] >= timedelta(days=4)


def test_seed_has_about_15_percent_delayed_shipments(session: Session) -> None:
    seed(session)

    shipments = session.scalars(select(Shipment)).all()
    delayed = sum(s.status == ShipmentStatus.DELAYED for s in shipments)
    assert 0.12 <= delayed / len(shipments) <= 0.18
