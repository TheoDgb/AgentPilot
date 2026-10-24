"""Fixtures : base PostgreSQL de test (`shopsim_test` du compose), une transaction par test."""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from shopsim.app import models  # noqa: F401  (enregistre les modèles)
from shopsim.app.db import Base, get_session
from shopsim.app.main import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim_test"
)


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
