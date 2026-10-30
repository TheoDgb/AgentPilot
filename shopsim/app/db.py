"""Connexion à PostgreSQL et session SQLAlchemy (synchrone)."""

import os
from collections.abc import Iterator

from sqlalchemy import Connection, Engine, create_engine, text
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


def add_enum_values(connection: Connection, type_name: str, values: list[str]) -> None:
    """Ajoute à un type énuméré PostgreSQL les valeurs qui lui manquent (connexion en
    autocommit : une valeur ajoutée dans une transaction n'est utilisable qu'après elle)."""
    for value in values:
        # Noms et valeurs du code, jamais d'une requête : pas de paramètre possible en DDL
        connection.execute(text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"))


def init_db(bind: Engine | None = None) -> None:
    """Crée les tables manquantes (pas d'Alembic pour l'instant), et met à niveau une base
    existante : `chaos_faults.started_real_at` remplace `request_count`, nouvelles pannes
    ajoutées au type `chaos_scenario`. Le seed recrée tout."""
    from shopsim.app import models

    bind = bind or engine
    Base.metadata.create_all(bind)
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        add_enum_values(connection, "chaos_scenario", [s.value for s in models.ChaosScenario])
    with bind.begin() as connection:
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
