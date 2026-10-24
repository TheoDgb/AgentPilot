from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.models import Customer, Order, OrderStatus
from shopsim.seed import N_CUSTOMERS, N_ORDERS, seed


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
