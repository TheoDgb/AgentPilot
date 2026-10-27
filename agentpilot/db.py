"""Base d'AgentPilot : même PostgreSQL que ShopSim, mais schéma séparé `agentpilot`.

La métadonnée est propre à AgentPilot : le seed de ShopSim (`drop_all` de ses propres
tables) n'y touche jamais.
"""

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateSchema

from agentpilot.env_file import setting

SCHEMA = "agentpilot"
# Environnement, sinon `.env` (cette ligne seulement)
DATABASE_URL = (
    setting("AGENTPILOT_DATABASE_URL")
    or "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim"
)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def make_session_factory(bind: Engine) -> sessionmaker[Session]:
    """Sessions du service, réglages par défaut de SQLAlchemy : un commit expire les objets, qui
    ne se relisent plus une fois leur session fermée. Les tests utilisent cette même fonction,
    pour qu'un objet lu hors de sa session y échoue comme en production."""
    return sessionmaker(bind=bind)


# Connexion paresseuse : rien n'est ouvert à l'import
engine = create_engine(DATABASE_URL)
SessionLocal = make_session_factory(engine)


def init_db(bind: Engine) -> None:
    """Crée le schéma, les tables manquantes (pas d'Alembic pour l'instant) et la ligne des
    interrupteurs de pilotage si elle manque (boucle en pause, pas d'arrêt d'urgence)."""
    from agentpilot import models

    with bind.begin() as connection:
        connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        Base.metadata.create_all(connection)
        connection.execute(
            insert(models.Control)
            .values(id=models.CONTROL_ID, loop_paused=True, emergency_stop=False)
            .on_conflict_do_nothing(index_elements=["id"])
        )
