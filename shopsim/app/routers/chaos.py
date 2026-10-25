"""Endpoints Chaos : activer, arrêter et lister les pannes (jeton obligatoire)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from shopsim.app.chaos import active_faults, require_chaos_token
from shopsim.app.db import get_session
from shopsim.app.models import ChaosFault, ChaosScenario
from shopsim.app.schemas import ChaosFaultRead, ChaosState

router = APIRouter(
    prefix="/chaos",
    tags=["chaos"],
    dependencies=[Depends(require_chaos_token)],
    responses={403: {"description": "Jeton X-Chaos-Token absent ou invalide"}},
)


@router.get("", response_model=ChaosState)
def list_faults(session: Annotated[Session, Depends(get_session)]) -> ChaosState:
    return ChaosState(active=[ChaosFaultRead.model_validate(f) for f in active_faults(session)])


@router.post(
    "/{scenario}",
    response_model=ChaosFaultRead,
    status_code=201,
    responses={200: {"description": "Panne déjà active : rien ne change"}},
)
def activate_fault(
    scenario: ChaosScenario,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> ChaosFault:
    # ON CONFLICT : deux activations simultanées ne créent pas d'erreur
    inserted = session.execute(
        insert(ChaosFault)
        .values(scenario=scenario, activated_at=datetime.now(UTC), request_count=0)
        .on_conflict_do_nothing()
        .returning(ChaosFault.scenario)
    ).scalar_one_or_none()
    session.commit()
    if inserted is None:
        response.status_code = 200
    return session.scalars(select(ChaosFault).where(ChaosFault.scenario == scenario)).one()


@router.delete("/{scenario}", status_code=204)
def stop_fault(scenario: ChaosScenario, session: Annotated[Session, Depends(get_session)]) -> None:
    # Arrêter une panne déjà arrêtée réussit aussi : rejeu sans effet
    session.execute(delete(ChaosFault).where(ChaosFault.scenario == scenario))
    session.commit()
