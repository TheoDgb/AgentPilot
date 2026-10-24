from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.models import Customer, Order, OrderStatus
from shopsim.seed import N_CUSTOMERS, N_ORDERS, PAYMENT_STATUS_BY_ORDER_STATUS, seed


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
