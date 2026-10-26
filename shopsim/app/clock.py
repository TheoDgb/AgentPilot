"""Horloge de la simulation : seule source de temps de ShopSim.

Le seed est figé au 2026-07-06 12:00 UTC (`SIMULATION_NOW`) : il ne dépend jamais de
l'horloge système et reste déterministe. Ensuite le temps avance au rythme réel :
`now(session)` vaut `SIMULATION_NOW` + le temps réel écoulé depuis le dernier seed, dont
l'heure réelle est l'ancre (table `simulation_clock`). Tous les processus partagent donc la
même date simulée, et elle ne recule pas au redémarrage de l'application.

Toute écriture datée (remboursement, réponse, renvoi, panne) utilise `now(session)`, jamais
`datetime.now()` : un test le vérifie dans tout le code de ShopSim.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopsim.app.models import SimulationClock

SIMULATION_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def simulation_today() -> date:
    return SIMULATION_NOW.date()


def real_now() -> datetime:
    """Seule lecture de l'horloge système ; les tests la remplacent pour figer le temps."""
    return datetime.now(UTC)


# Origine de repli si la base n'a jamais été seedée : le démarrage du processus
_PROCESS_START = real_now()


def start(session: Session) -> None:
    """Remet la simulation au 2026-07-06 12:00 (appelé par le seed)."""
    session.merge(SimulationClock(id=1, started_at=real_now()))


def now(session: Session) -> datetime:
    """Date simulée : `SIMULATION_NOW` + temps réel écoulé depuis le dernier seed."""
    started_at = session.scalar(select(SimulationClock.started_at).where(SimulationClock.id == 1))
    elapsed = real_now() - (started_at or _PROCESS_START)
    # Jamais avant le début de la simulation, même si l'horloge système recule
    return SIMULATION_NOW + max(elapsed, timedelta(0))
