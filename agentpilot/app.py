"""Service AgentPilot : lance les agents et enregistre leurs exécutions.

Lancement (local) : `.venv/bin/uvicorn agentpilot.app:app --port 8001`.

- `POST /runs/support/{ticket_id}` lance l'agent Support en tâche de fond (202), avec les
  outils d'action sous hooks : les actions sensibles sont mises en attente d'un humain ;
- `GET /runs/{id}` et `GET /runs` renvoient les exécutions enregistrées, avec leur journal
  d'appels d'outils et leurs actions en attente ;
- `POST /checks/support` vérifie l'isolement de l'agent (consigne, politique, outils) ;
- `/approvals` : pages d'approbation des actions en attente (`agentpilot/web.py`) ;
- `/control` : page de pilotage (pause de la boucle, arrêt d'urgence, budget) ;
  `GET /control/status` en JSON.

Au démarrage, une boucle de fond (`agentpilot/worker.py`) traite les tickets ouverts et
reprend ce qui a été interrompu ; elle démarre en pause au premier lancement.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import sdk_runner, web
from agentpilot.api_schemas import ControlStatus, RunCreated, RunRead
from agentpilot.db import SessionLocal, engine, init_db
from agentpilot.journal import DbJournal
from agentpilot.models import AgentKind, Run, RunTrigger
from agentpilot.worker import AlreadyRunning, OverBudget, Worker, mark_interrupted, new_run

__all__ = ["app", "mark_interrupted"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db(engine)
    with app.state.session_factory() as session:
        # Reprise : le premier tour de la boucle relance ces tickets
        mark_interrupted(session)
    worker: Worker = app.state.worker
    serving = asyncio.create_task(worker.serve())
    yield
    # Ctrl+C ou arrêt du service : exécutions annulées (CLI arrêtée), marquées interrupted
    await worker.shutdown()
    await serving


app = FastAPI(title="AgentPilot", lifespan=lifespan)
app.include_router(web.router)
app.include_router(web.control_router)
# Remplaçables par les tests : sessions de base et fonction `query` du SDK
app.state.session_factory = SessionLocal
app.state.query = sdk_runner.sdk_query
# Boucle de traitement et exécutions en cours (au plus 2, endpoint manuel compris)
app.state.worker = Worker(app.state)


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@app.post("/runs/support/{ticket_id}", status_code=202, response_model=RunCreated)
async def start_support_run(
    ticket_id: int, request: Request, background: BackgroundTasks
) -> RunCreated:
    """Lance l'agent sur un ticket : 409 s'il est déjà en cours, 429 si le budget du jour est
    atteint."""
    worker: Worker = request.app.state.worker
    try:
        run, launch = await worker.start_ticket(ticket_id, RunTrigger.MANUAL)
    except AlreadyRunning:
        raise HTTPException(status_code=409, detail="Ticket already being processed") from None
    except OverBudget:
        raise HTTPException(status_code=429, detail="Daily budget reached") from None
    background.add_task(worker.execute, run.id, ticket_id, RunTrigger.MANUAL, launch)
    return RunCreated(id=run.id, status=run.status)


@app.post("/checks/support", response_model=RunRead)
async def check_support(request: Request, session: SessionDep) -> Run:
    """Vérification d'isolement, exécutée tout de suite (courte) et enregistrée."""
    worker: Worker = request.app.state.worker
    factory = request.app.state.session_factory
    run = await asyncio.to_thread(
        new_run, factory, AgentKind.SUPPORT_CHECK, None, RunTrigger.MANUAL, check_budget=False
    )
    query = request.app.state.query
    journal = DbJournal(factory, run.id)
    await worker.execute(
        run.id,
        None,
        RunTrigger.MANUAL,
        lambda: sdk_runner.check_context(journal=journal, query=query),
    )
    session.expire_all()
    return session.get_one(Run, run.id)


@app.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: uuid.UUID, session: SessionDep) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/runs", response_model=list[RunRead])
def list_runs(
    session: SessionDep,
    ticket_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[Run]:
    query = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if ticket_id is not None:
        query = query.where(Run.ticket_id == ticket_id)
    return list(session.scalars(query))


@app.get("/control/status", response_model=ControlStatus)
def control_status(request: Request) -> ControlStatus:
    """État de la boucle, des interrupteurs, du budget du jour et de ShopSim."""
    return web.control_status(request.app.state.worker, request.app.state.session_factory)
