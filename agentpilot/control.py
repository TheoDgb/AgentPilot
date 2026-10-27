"""Pilotage de l'agent Support : pause de la boucle, arrêt d'urgence, budget du jour.

Les interrupteurs sont en base (ligne unique `agentpilot.control`) : ils survivent à un
redémarrage, et les hooks relisent l'arrêt d'urgence avant chaque action.

Budget : la dépense du jour est la somme des coûts des exécutions créées depuis 00:00 UTC
(heure réelle de la plateforme, pas celle de la simulation). Un coût inconnu compte pour le
plafond d'une exécution (`MAX_BUDGET_USD`) : exécution en cours, interrompue, ou en échec après
le démarrage de l'agent. Une exécution n'est lancée que si la dépense du jour plus ce plafond
reste sous le budget : le budget n'est jamais dépassé.

Fonctions synchrones : à appeler hors de la boucle d'événements (`asyncio.to_thread`).
"""

from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.decisions import NAME_MAX, clean_text
from agentpilot.env_file import setting
from agentpilot.models import CONTROL_ID, Control, PauseReason, Run, RunStatus
from agentpilot.sdk_runner import MAX_BUDGET_USD

BUDGET_ENV = "AGENTPILOT_DAILY_BUDGET_USD"
DEFAULT_DAILY_BUDGET_USD = Decimal("5")
# Coût retenu pour une exécution dont le coût n'est pas (encore) connu
RUN_RESERVE_USD = Decimal(str(MAX_BUDGET_USD))


def _now() -> datetime:
    return datetime.now(UTC)


def daily_budget() -> Decimal:
    """Plafond de dépense par jour, en dollars : environnement, sinon `.env` (relu à chaque
    appel)."""
    value = setting(BUDGET_ENV)
    return Decimal(value) if value else DEFAULT_DAILY_BUDGET_USD


def load(factory: sessionmaker[Session]) -> Control:
    with factory() as session:
        return session.get_one(Control, CONTROL_ID)


def emergency_stop(factory: sessionmaker[Session]) -> bool:
    with factory() as session:
        return bool(session.scalar(select(Control.emergency_stop).where(Control.id == CONTROL_ID)))


def spent_today(session: Session, now: datetime | None = None) -> Decimal:
    """Dépense du jour (UTC), coûts inconnus comptés au plafond d'une exécution."""
    start = datetime.combine((now or _now()).date(), time(), tzinfo=UTC)
    unknown = Run.total_cost_usd.is_(None) & (
        Run.status.in_([RunStatus.RUNNING, RunStatus.INTERRUPTED])
        # Échec après le démarrage de l'agent (modèle connu à l'initialisation) : il a pu
        # consommer
        | and_(Run.status == RunStatus.FAILED, Run.model.is_not(None))
    )
    cost = case(
        (Run.total_cost_usd.is_not(None), Run.total_cost_usd),
        (unknown, RUN_RESERVE_USD),
        else_=Decimal(0),
    )
    total = session.scalar(select(func.coalesce(func.sum(cost), 0)).where(Run.created_at >= start))
    return Decimal(total)


def budget_allows(session: Session, now: datetime | None = None) -> bool:
    """Une exécution de plus, au pire à son plafond, reste-t-elle dans le budget du jour ?"""
    return spent_today(session, now) + RUN_RESERVE_USD <= daily_budget()


def _name(name: str | None) -> str | None:
    return clean_text(name or "", "nom", NAME_MAX, required=True)


def _update(factory: sessionmaker[Session], **values: object) -> None:
    with factory() as session:
        session.execute(update(Control).where(Control.id == CONTROL_ID).values(**values))
        session.commit()


def pause(factory: sessionmaker[Session], name: str | None, reason: PauseReason) -> None:
    """Met la boucle en pause ; le nom est obligatoire sauf pour la pause automatique du budget.
    Lève `InvalidInput`."""
    by = _name(name) if reason is PauseReason.MANUAL else None
    _update(factory, loop_paused=True, pause_reason=reason, paused_by=by, paused_at=_now())


def resume(factory: sessionmaker[Session], name: str) -> bool:
    """Relance la boucle ; False (rien ne change) si le budget du jour est atteint.
    Lève `InvalidInput`."""
    by = _name(name)
    with factory() as session:
        if not budget_allows(session):
            return False
    _update(factory, loop_paused=False, pause_reason=None, paused_by=by, paused_at=_now())
    return True


def set_emergency(factory: sessionmaker[Session], on: bool, name: str) -> None:
    """Active ou lève l'arrêt d'urgence. Lève `InvalidInput`."""
    by = _name(name)
    _update(factory, emergency_stop=on, emergency_by=by, emergency_at=_now())
