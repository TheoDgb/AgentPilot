"""Service AgentPilot : lance les agents et enregistre leurs exécutions.

Lancement (local) : `.venv/bin/uvicorn agentpilot.app:app --port 8001`.

- `POST /runs/support/{ticket_id}` lance l'agent Support en tâche de fond (202) ;
- `GET /runs/{id}` et `GET /runs` renvoient les exécutions enregistrées ;
- `POST /checks/support` vérifie l'isolement de l'agent (consigne, politique, outils).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import sdk_runner
from agentpilot.api_schemas import RunCreated, RunRead
from agentpilot.db import SessionLocal, engine, init_db
from agentpilot.models import AgentKind, Run, RunStatus
from agentpilot.sdk_runner import AgentResult

# Au plus 2 agents en même temps : coût et charge de ShopSim bornés
MAX_CONCURRENT_RUNS = 2
_slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


def mark_interrupted(session: Session) -> int:
    """Exécutions restées `running` (service arrêté pendant leur exécution)."""
    count = session.execute(
        update(Run)
        .where(Run.status == RunStatus.RUNNING)
        .values(status=RunStatus.INTERRUPTED, finished_at=datetime.now(UTC))
    ).rowcount
    session.commit()
    return count


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db(engine)
    with app.state.session_factory() as session:
        mark_interrupted(session)
    yield


app = FastAPI(title="AgentPilot", lifespan=lifespan)
# Remplaçables par les tests : sessions de base et fonction `query` du SDK
app.state.session_factory = SessionLocal
app.state.query = sdk_runner.sdk_query


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _new_run(session: Session, agent: AgentKind, ticket_id: int | None) -> Run:
    run = Run(
        agent=agent,
        ticket_id=ticket_id,
        status=RunStatus.RUNNING,
        tool_calls=[],
        created_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return run


def _finish(session: Session, run: Run, result: AgentResult) -> None:
    run.status = RunStatus.FAILED if result.error else RunStatus.SUCCEEDED
    run.simulation_date = result.simulation_date
    run.model = result.model
    run.output = result.output
    run.error = result.error
    run.total_cost_usd = (
        Decimal(str(result.total_cost_usd)) if result.total_cost_usd is not None else None
    )
    run.duration_ms = result.duration_ms
    run.num_turns = result.num_turns
    run.session_id = result.session_id
    run.available_tools = result.available_tools
    run.tool_calls = result.tool_calls
    run.finished_at = datetime.now(UTC)
    session.commit()


async def _execute(
    factory: sessionmaker[Session], run_id: uuid.UUID, launch: Callable[[], Awaitable[AgentResult]]
) -> None:
    async with _slots:
        try:
            result = await launch()
        except Exception as exc:  # toute panne inattendue est enregistrée
            result = AgentResult(error=f"Erreur interne d'AgentPilot : {exc!r}")
    with factory() as session:
        run = session.get(Run, run_id)
        if run is not None:
            _finish(session, run, result)


@app.post("/runs/support/{ticket_id}", status_code=202, response_model=RunCreated)
def start_support_run(
    ticket_id: int, request: Request, session: SessionDep, background: BackgroundTasks
) -> RunCreated:
    run = _new_run(session, AgentKind.SUPPORT, ticket_id)
    query = request.app.state.query
    background.add_task(
        _execute,
        request.app.state.session_factory,
        run.id,
        lambda: sdk_runner.decide_ticket(ticket_id, query=query),
    )
    return RunCreated(id=run.id, status=run.status)


@app.post("/checks/support", response_model=RunRead)
async def check_support(request: Request, session: SessionDep) -> Run:
    """Vérification d'isolement, exécutée tout de suite (courte) et enregistrée."""
    run = _new_run(session, AgentKind.SUPPORT_CHECK, None)
    query = request.app.state.query
    await _execute(
        request.app.state.session_factory,
        run.id,
        lambda: sdk_runner.check_context(query=query),
    )
    session.refresh(run)
    return run


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
