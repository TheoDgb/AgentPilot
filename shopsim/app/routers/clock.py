"""Horloge de la simulation, lisible par les clients de ShopSim (serveur MCP, AgentPilot).

Hors pannes Chaos : la date de référence reste disponible pendant un incident.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from shopsim.app import clock
from shopsim.app.db import get_session
from shopsim.app.schemas import ClockRead

router = APIRouter(prefix="/clock", tags=["clock"])


@router.get("", response_model=ClockRead)
def get_clock(session: Annotated[Session, Depends(get_session)]) -> ClockRead:
    return ClockRead(now=clock.now(session), frozen=clock.is_frozen())
