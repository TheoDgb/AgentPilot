"""Fixtures : base PostgreSQL de test (`shopsim_test` du compose), une transaction par test."""

import itertools
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from shopsim.app.db import Base, get_session
from shopsim.app.main import app
from shopsim.app.models import Customer, Order, OrderStatus, Payment, PaymentStatus

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim_test"
)

_ORDER_STATUS_BY_PAYMENT_STATUS = {
    PaymentStatus.PENDING: OrderStatus.PENDING,
    PaymentStatus.SUCCEEDED: OrderStatus.PAID,
    PaymentStatus.FAILED: OrderStatus.CANCELLED,
    PaymentStatus.REFUNDED: OrderStatus.REFUNDED,
}
_unique = itertools.count(1)


@pytest.fixture(autouse=True)
def no_payment_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pas de latence simulée dans les tests."""
    monkeypatch.setenv("PAYMENT_LATENCY_MS", "0")


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.fail(
            f"Base de test injoignable ({TEST_DATABASE_URL}). "
            f"Lancer `docker compose up -d db`.\n{exc}",
            pytrace=False,
        )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine: Engine) -> Iterator[Session]:
    """Session dont tout est annulé en fin de test."""
    with db_engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as s:
            yield s
        transaction.rollback()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    # Pas de `with` : le lifespan (create_all sur la base principale) n'est pas exécuté
    yield TestClient(app)
    app.dependency_overrides.clear()


MakePayment = Callable[..., Payment]


@pytest.fixture
def make_payment(session: Session) -> MakePayment:
    """Crée client + commande + paiement (statut de commande cohérent avec le paiement)."""

    def _make(
        amount_cents: int = 10_000, status: PaymentStatus = PaymentStatus.SUCCEEDED
    ) -> Payment:
        n = next(_unique)
        created = datetime(2026, 3, 1, tzinfo=UTC)
        customer = Customer(
            email=f"client.{n}@example.com",
            first_name="Test",
            last_name="Client",
            created_at=created,
        )
        order = Order(
            customer=customer,
            status=_ORDER_STATUS_BY_PAYMENT_STATUS[status],
            total_cents=amount_cents,
            currency="EUR",
            created_at=created,
        )
        payment = Payment(order=order, status=status, amount_cents=amount_cents, created_at=created)
        session.add(payment)
        session.flush()
        return payment

    return _make
