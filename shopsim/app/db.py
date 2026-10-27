"""Connexion à PostgreSQL et session SQLAlchemy (synchrone)."""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim"
)

# Une erreur SQL ne recopie pas les valeurs (texte des messages clients) dans son message
engine = create_engine(DATABASE_URL, hide_parameters=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
    """Dépendance FastAPI : une session par requête."""
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Crée les tables manquantes (pas d'Alembic pour l'instant)."""
    from shopsim.app import models  # noqa: F401  (enregistre les modèles)

    Base.metadata.create_all(engine)
