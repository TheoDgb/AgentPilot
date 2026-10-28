"""Service AgentPilot : lance les agents et enregistre leurs exécutions.

Lancement (local) : `.venv/bin/uvicorn agentpilot.app:app --port 8001`.

- `POST /runs/support/{ticket_id}` lance l'agent Support en tâche de fond (202), avec les
  outils d'action sous hooks : les actions sensibles sont mises en attente d'un humain ;
- `GET /runs/{id}` et `GET /runs` renvoient les exécutions enregistrées, avec leur journal
  d'appels d'outils et leurs actions en attente ;
- `POST /checks/support` vérifie l'isolement de l'agent (consigne, politique, outils) ;
- `/approvals` : pages d'approbation des actions en attente (`agentpilot/web.py`) ;
- `/control` : page de pilotage (pause de la boucle, arrêt d'urgence, budget) ;
  `GET /control/status` en JSON ;
- `POST /alerts/webhook` : alertes d'Alertmanager, enregistrées sans action automatique ;
  `GET /alerts` pour les relire (`agentpilot/alerts.py`) ;
- `GET /metrics` : métriques Prometheus (`agentpilot/metrics.py`), collectées toutes les 10 s,
  non journalisées.

Au démarrage, le service prend le verrou de la base (`agentpilot.db.service_lock`) et refuse
de démarrer s'il est déjà tenu : un seul service par base, et jamais pendant une remise à zéro.

Au démarrage, une boucle de fond (`agentpilot/worker.py`) traite les tickets ouverts et
reprend ce qui a été interrompu ; elle démarre en pause au premier lancement.

Logs JSON (`observability`) sur stderr et dans `<AGENTPILOT_LOG_DIR>/agentpilot.jsonl`
(`logs/` à la racine du dépôt par défaut), configurés au démarrage : les tests n'écrivent
aucun fichier sans le demander.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import alerts, metrics, sdk_runner, web
from agentpilot.api_schemas import ControlStatus, RunCreated, RunRead
from agentpilot.db import SessionLocal, engine, init_db, service_lock
from agentpilot.env_file import setting
from agentpilot.journal import DbJournal
from agentpilot.models import AgentKind, Run, RunTrigger
from agentpilot.support_agent import REPO_ROOT
from agentpilot.worker import AlreadyRunning, OverBudget, Worker, mark_interrupted, new_run
from observability.http import RequestLogMiddleware
from observability.logs import configure, error_type, log_event
from observability.metrics import CONTENT_TYPE_LATEST, exposition

__all__ = ["app", "mark_interrupted"]

LOG_DIR_ENV = "AGENTPILOT_LOG_DIR"
METRICS_PATH = "/metrics"
logger = logging.getLogger(__name__)


def log_dir() -> Path:
    """Dossier des logs JSON des trois services en local (aussi donné au serveur MCP).

    Un chemin relatif part de la racine du dépôt, pas du dossier courant : le serveur MCP
    tourne dans le dossier de travail temporaire de l'exécution."""
    return REPO_ROOT / (setting(LOG_DIR_ENV) or "logs")


class ServiceAlreadyRunning(RuntimeError):
    """Le verrou du service est tenu : autre service AgentPilot, ou remise à zéro en cours."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure("agentpilot", log_dir())
    init_db(engine)
    # Un seul service par base, et jamais pendant une remise à zéro (`agentpilot.reset`)
    with service_lock(engine) as locked:
        if not locked:
            raise ServiceAlreadyRunning(
                "Un autre service AgentPilot, ou une remise à zéro (python -m agentpilot.reset), "
                "utilise déjà cette base."
            )
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
# Collecte de Prometheus toutes les 10 s : journalisée seulement en cas d'échec
app.add_middleware(
    RequestLogMiddleware,
    logger=logging.getLogger("agentpilot.http"),
    quiet_paths=frozenset({METRICS_PATH}),
)
app.include_router(web.router)
app.include_router(web.control_router)
app.include_router(alerts.router)
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
    background.add_task(worker.execute, run, launch)
    return RunCreated(id=run.id, status=run.status, trace_id=run.trace_id)


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
    await worker.execute(run, lambda: sdk_runner.check_context(journal=journal, query=query))
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


@app.get(METRICS_PATH, response_class=Response)
def get_metrics(request: Request) -> Response:
    """Métriques Prometheus : événements du processus et état relu en base. Base injoignable :
    `agentpilot_state_up 0`, les compteurs restent collectés."""
    work: Worker = request.app.state.worker
    factory: sessionmaker[Session] = request.app.state.session_factory
    try:
        status = web.control_status(work, factory)
        with factory() as session:
            pending = metrics.pending_counts(session)
            oldest = metrics.oldest_pending(session)
    except SQLAlchemyError as exc:
        log_event(logger, "metrics.state_unavailable", logging.WARNING, error_type=error_type(exc))
        status, pending, oldest = None, None, None
    state = metrics.state_registry(status, pending, work.clock(), oldest)
    return Response(exposition(metrics.EVENTS, state), media_type=CONTENT_TYPE_LATEST)
