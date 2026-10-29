"""Connexion à PostgreSQL et session SQLAlchemy (synchrone)."""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, text
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
    """Crée les tables manquantes (pas d'Alembic pour l'instant), et met à niveau une base
    existante : `chaos_faults.started_real_at` remplace `request_count`. Le seed recrée tout."""
    from shopsim.app import models  # noqa: F401  (enregistre les modèles)

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        # Une panne d'avant cette colonne n'a pas d'heure réelle : supprimée, à réactiver
        connection.execute(
            text(
                "ALTER TABLE chaos_faults ADD COLUMN IF NOT EXISTS started_real_at "
                "TIMESTAMP WITH TIME ZONE"
            )
        )
        connection.execute(text("DELETE FROM chaos_faults WHERE started_real_at IS NULL"))
        connection.execute(
            text("ALTER TABLE chaos_faults ALTER COLUMN started_real_at SET NOT NULL")
        )
        connection.execute(text("ALTER TABLE chaos_faults DROP COLUMN IF EXISTS request_count"))
