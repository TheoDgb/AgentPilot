"""Base d'AgentPilot : même PostgreSQL que ShopSim, mais schéma séparé `agentpilot`.

La métadonnée est propre à AgentPilot : le seed de ShopSim (`drop_all` de ses propres
tables) n'y touche jamais.
"""

import os

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.schema import CreateSchema

SCHEMA = "agentpilot"
DATABASE_URL = os.environ.get(
    "AGENTPILOT_DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim"
)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


# Connexion paresseuse : rien n'est ouvert à l'import
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


def init_db(bind: Engine) -> None:
    """Crée le schéma et les tables manquantes (pas d'Alembic pour l'instant)."""
    from agentpilot import models  # noqa: F401  (enregistre les modèles)

    with bind.begin() as connection:
        connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        Base.metadata.create_all(connection)
