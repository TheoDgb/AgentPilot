"""Pannes injectées dans ShopSim (J1.5).

L'état des pannes est en base (table `chaos_faults`) : une panne activée par un
processus est vue par tous les autres. Les endpoints Chaos exigent le jeton
`CHAOS_TOKEN`, que les agents n'ont jamais.

Les erreurs écrites dans les logs imitent exactement de vraies erreurs, sans
mention de la panne : seul `GET /chaos` dit quelle panne est active.
"""

import logging
import os
import secrets
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shopsim.app.db import get_session
from shopsim.app.models import ChaosFault, ChaosScenario

logger = logging.getLogger("shopsim")

# payment_latency : temps de réponse du service Paiements pendant la panne
CHAOS_PAYMENT_LATENCY_MS = 4_000
# connection_saturation : attente de n × 50 ms à la n-ième requête, plafonnée à 5 s
SATURATION_STEP_MS = 50
SATURATION_MAX_MS = 5_000


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


def _internal_error() -> HTTPException:
    return HTTPException(status_code=500, detail="Internal Server Error")


def raise_if_db_errors(session: Session) -> None:
    """db_errors : les lectures des commandes échouent comme sur une base indisponible."""
    if is_active(session, ChaosScenario.DB_ERRORS):
        logger.error(
            "psycopg.OperationalError: server closed the connection unexpectedly\n"
            "\tThis probably means the server terminated abnormally\n"
            "\tbefore or while processing the request."
        )
        raise _internal_error()


def raise_if_bad_deploy(session: Session) -> None:
    """bad_deploy : seuls les remboursements échouent, comme après une mauvaise configuration."""
    if is_active(session, ChaosScenario.BAD_DEPLOY):
        logger.error("KeyError: 'REFUND_PROVIDER_URL'")
        raise _internal_error()


def apply_connection_saturation(session: Annotated[Session, Depends(get_session)]) -> None:
    """connection_saturation : chaque requête attend un peu plus que la précédente."""
    count = session.execute(
        update(ChaosFault)
        .where(ChaosFault.scenario == ChaosScenario.CONNECTION_SATURATION)
        .values(request_count=ChaosFault.request_count + 1)
        .returning(ChaosFault.request_count)
    ).scalar_one_or_none()
    if count is None:
        return
    session.commit()
    time.sleep(min(count * SATURATION_STEP_MS, SATURATION_MAX_MS) / 1000)
