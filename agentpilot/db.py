"""Base d'AgentPilot : même PostgreSQL que ShopSim, mais schéma séparé `agentpilot`.

La métadonnée est propre à AgentPilot : le seed de ShopSim (`drop_all` de ses propres
tables) n'y touche jamais.

Verrou du service (`service_lock`) : un verrou consultatif PostgreSQL, tenu par le service tout
au long de sa vie sur une connexion dédiée. Un second service, ou une remise à zéro
(`python -m agentpilot.reset`), ne l'obtient pas et refuse de tourner. PostgreSQL le libère dès
que la connexion se ferme, même après un `kill -9` du service.
"""

import contextlib
from collections.abc import Iterator

from sqlalchemy import Engine, MetaData, create_engine, func, select, text, update
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


# Identifiant du verrou consultatif (« AGENTPIL » en ASCII), propre à chaque base
SERVICE_LOCK_ID = 0x4147454E5450494C
# Connexion paresseuse : rien n'est ouvert à l'import
# Une erreur SQL ne recopie pas les valeurs (texte des messages clients) dans son message
engine = create_engine(DATABASE_URL, hide_parameters=True)
SessionLocal = make_session_factory(engine)


# Valeurs ajoutées aux énumérations après leur création (pas de migrations)
ENUM_ADDITIONS = (
    ("run_trigger", "eval"),
    ("pause_reason", "infra"),
    ("agent_kind", "ops"),
    ("agent_kind", "ops_check"),
)
# Colonnes ajoutées aux tables après leur création
COLUMN_ADDITIONS = (
    ("runs", "error_type", "VARCHAR(32)"),
    ("control", "pause_detail", "VARCHAR(32)"),
    ("runs", "alert_id", f"UUID REFERENCES {SCHEMA}.alerts(id)"),
)


def init_db(bind: Engine) -> None:
    """Crée le schéma, les tables manquantes (pas d'Alembic pour l'instant) et la ligne des
    interrupteurs de pilotage si elle manque (boucle en pause, pas d'arrêt d'urgence), puis met
    à niveau une base existante (valeurs d'énumérations, colonnes, index)."""
    from agentpilot import models

    with bind.begin() as connection:
        connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        Base.metadata.create_all(connection)
        connection.execute(
            insert(models.Control)
            .values(id=models.CONTROL_ID, loop_paused=True, emergency_stop=False)
            .on_conflict_do_nothing(index_elements=["id"])
        )
    # Une valeur d'énumération ajoutée ne peut pas servir dans sa propre transaction (l'index
    # partiel des enquêtes l'utilise) : chacune est validée tout de suite
    with bind.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        for enum, value in ENUM_ADDITIONS:
            connection.execute(
                text(f"ALTER TYPE {SCHEMA}.{enum} ADD VALUE IF NOT EXISTS '{value}'")
            )
    with bind.begin() as connection:
        for table, column, kind in COLUMN_ADDITIONS:
            connection.execute(
                text(f"ALTER TABLE {SCHEMA}.{table} ADD COLUMN IF NOT EXISTS {column} {kind}")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_running_run_per_alert "
                f"ON {SCHEMA}.runs (alert_id) WHERE status = 'running' AND agent = 'ops'"
            )
        )


@contextlib.contextmanager
def service_lock(bind: Engine) -> Iterator[bool]:
    """Prend le verrou du service pour la durée du bloc ; False s'il est déjà tenu ailleurs."""
    with bind.connect() as connection:
        locked = bool(connection.scalar(select(func.pg_try_advisory_lock(SERVICE_LOCK_ID))))
        # Verrou de session : il survit à la transaction, qu'on ne laisse pas ouverte
        connection.commit()
        try:
            yield locked
        finally:
            if locked:
                # Connexion perdue entre-temps : PostgreSQL a déjà libéré le verrou
                with contextlib.suppress(Exception):
                    connection.scalar(select(func.pg_advisory_unlock(SERVICE_LOCK_ID)))
                    connection.commit()


def clear(bind: Engine, loop_paused: bool = True) -> None:
    """Vide les exécutions, le journal, les actions en attente et les alertes reçues, et remet
    les interrupteurs à leur état de départ (boucle en pause, pas d'arrêt d'urgence)."""
    from agentpilot import models

    with bind.begin() as connection:
        connection.execute(
            text(
                f"TRUNCATE {SCHEMA}.tool_calls, {SCHEMA}.pending_actions, {SCHEMA}.runs, "
                f"{SCHEMA}.alerts"
            )
        )
        connection.execute(
            update(models.Control).values(
                loop_paused=loop_paused,
                pause_reason=None,
                pause_detail=None,
                paused_by=None,
                paused_at=None,
                emergency_stop=False,
                emergency_by=None,
                emergency_at=None,
            )
        )
