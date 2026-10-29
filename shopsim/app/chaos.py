"""Pannes injectées dans ShopSim (J1.5).

L'état des pannes est en base (table `chaos_faults`) : une panne activée par un
processus est vue par tous les autres. Les endpoints Chaos exigent le jeton
`CHAOS_TOKEN`, que les agents n'ont jamais.

Les erreurs écrites dans les logs imitent exactement de vraies erreurs, sans
mention de la panne : seul `GET /chaos` dit quelle panne est active. Même événement
(`http.exception`), même logger et même format JSON qu'une erreur réelle.
"""

import logging
import os
import secrets
import time
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from observability.logs import log_event
from shopsim.app import clock
from shopsim.app.db import get_session
from shopsim.app.models import ChaosFault, ChaosScenario

logger = logging.getLogger("shopsim")

# payment_latency : temps de réponse du service Paiements pendant la panne
CHAOS_PAYMENT_LATENCY_MS = 4_000
# connection_saturation : attente qui monte de 0 à 5 s en 10 min de temps réel, puis reste à 5 s.
# Selon le temps écoulé, pas le nombre de requêtes : la montée ne dépend pas du débit
SATURATION_MAX_MS = 5_000
SATURATION_RAMP = timedelta(minutes=10)


def is_active(session: Session, scenario: ChaosScenario) -> bool:
    found = session.scalar(select(ChaosFault.scenario).where(ChaosFault.scenario == scenario))
    return found is not None


def active_faults(session: Session) -> list[ChaosFault]:
    return list(session.scalars(select(ChaosFault).order_by(ChaosFault.activated_at)))


def require_chaos_token(
    token: Annotated[str | None, Header(alias="X-Chaos-Token")] = None,
) -> None:
    """Sans `CHAOS_TOKEN` côté serveur, Chaos est fermé à tous."""
    expected = os.environ.get("CHAOS_TOKEN", "")
    if not expected or token is None or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid chaos token")


def _log_error(error_type: str, error: str) -> None:
    log_event(logger, "http.exception", logging.ERROR, error_type=error_type, error=error)


def _internal_error() -> HTTPException:
    return HTTPException(status_code=500, detail="Internal Server Error")


def raise_if_db_errors(session: Session) -> None:
    """db_errors : les lectures des commandes échouent comme sur une base indisponible."""
    if is_active(session, ChaosScenario.DB_ERRORS):
        _log_error(
            "psycopg.OperationalError",
            "server closed the connection unexpectedly\n"
            "\tThis probably means the server terminated abnormally\n"
            "\tbefore or while processing the request.",
        )
        raise _internal_error()


def raise_if_bad_deploy(session: Session) -> None:
    """bad_deploy : seuls les remboursements échouent, comme après une mauvaise configuration."""
    if is_active(session, ChaosScenario.BAD_DEPLOY):
        _log_error("KeyError", "'REFUND_PROVIDER_URL'")
        raise _internal_error()


def saturation_delay_s(elapsed: timedelta) -> float:
    """Attente d'une requête, `elapsed` après l'activation : montée linéaire jusqu'au plafond."""
    share = min(1.0, max(0.0, elapsed / SATURATION_RAMP))
    return share * SATURATION_MAX_MS / 1000


def apply_connection_saturation(session: Annotated[Session, Depends(get_session)]) -> None:
    """connection_saturation : les requêtes ralentissent peu à peu, comme une base dont les
    connexions s'épuisent ; à un instant donné, toutes attendent la même durée."""
    started = session.scalar(
        select(ChaosFault.started_real_at).where(
            ChaosFault.scenario == ChaosScenario.CONNECTION_SATURATION
        )
    )
    if started is None:
        return
    # Rien ne reste ouvert pendant l'attente
    session.commit()
    delay = saturation_delay_s(clock.real_now() - started)
    if delay > 0:
        time.sleep(delay)
