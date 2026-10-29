"""Boucle de fond : traitement continu de la file de tickets par l'agent Support.

Un tour (`Worker.tick`) :
1. ShopSim répond-il (`GET /clock`) ? Sinon, attente croissante (10 s, 20 s… 5 min) au lieu
   d'enchaîner les échecs ; rien n'est lancé ni réessayé.
2. Réessai des actions approuvées restées sans résultat, avec leur clé d'idempotence (sauf
   pendant l'arrêt d'urgence). Même boucle en pause : c'est la fin d'une décision humaine.
3. Boucle en pause : rien de plus. Budget du jour atteint : pause automatique.
4. Tickets ouverts dans ShopSim, les plus anciens d'abord, lancés dans les places libres (au
   plus `MAX_CONCURRENT_RUNS` exécutions en même temps, endpoint manuel compris). Un ticket
   n'est pas relancé s'il est en cours, s'il a été traité, si une action de ce ticket a été mise
   en attente d'un humain (quel que soit son statut), ou après `MAX_ATTEMPTS` échecs.

Jamais deux exécutions d'un même ticket en parallèle : index unique partiel en base
(`one_running_run_per_ticket`), quel que soit le déclencheur ou le processus.

Reprise : au démarrage, les exécutions restées `running` passent `interrupted` et leur ticket
est relancé ; la consigne de la relance liste les actions déjà faites par les exécutions
inachevées depuis la dernière réussie (journal). Les clés d'idempotence, qui ne dépendent
que du ticket et de l'action, garantissent qu'aucune action n'est faite deux fois : ShopSim
reconnaît un rejeu.

Arrêt du service : chaque exécution est annulée par une portée anyio, qui laisse le SDK arrêter
la CLI (voir `sdk_runner`), puis est marquée `interrupted`.

Logs : chaque exécution lie sa trace (`runs.trace_id`), son id et son ticket à tout ce qu'elle
écrit, jusqu'au serveur MCP et à ShopSim (`run.started`, `run.finished`) ; chaque tour de
boucle a sa propre trace (`loop.backoff`, `loop.paused`).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import anyio
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import control, decisions, metrics, sdk_runner
from agentpilot.approval import ESCALATE, REFUND, REPLY, RESEND, RESOLVE, short_name
from agentpilot.decisions import Notice
from agentpilot.env_file import setting
from agentpilot.journal import DbJournal
from agentpilot.models import (
    AgentKind,
    CallOutcome,
    PauseReason,
    PendingAction,
    Run,
    RunStatus,
    RunTrigger,
    ToolCall,
)
from agentpilot.sdk_runner import AgentResult
from agentpilot.support_agent import ACTION_TOOL_NAMES, DEFAULT_PROMPT
from mcp_server.shopsim_client import ShopSimUnavailable, get_json
from observability import trace
from observability.logs import log_event

logger = logging.getLogger(__name__)

# Coût et charge de ShopSim bornés
MAX_CONCURRENT_RUNS = 2
# Échecs ou interruptions d'un ticket avant abandon (affiché sur la page de pilotage)
MAX_ATTEMPTS = 3
# Délai avant de relancer un ticket dont l'exécution a échoué
RETRY_DELAY = timedelta(minutes=5)
# Exécutions en échec d'affilée avant de marquer une pause
MAX_FAILURES_IN_ROW = 3
BACKOFF_BASE_S = 10.0
BACKOFF_MAX_S = 300.0
INTERVAL_ENV = "AGENTPILOT_LOOP_INTERVAL_S"
DEFAULT_INTERVAL_S = 30.0
SHUTDOWN_TIMEOUT_S = 30.0
TICKETS_PAGE = 100

Launch = Callable[[], Awaitable[AgentResult]]


class AlreadyRunning(Exception):
    """Une exécution de ce ticket est déjà en cours."""


class OverBudget(Exception):
    """Le budget du jour ne permet pas une exécution de plus."""


def interval_s() -> float:
    """Délai entre deux tours : environnement, sinon `.env` (relu à chaque tour)."""
    return float(setting(INTERVAL_ENV) or DEFAULT_INTERVAL_S)


def _now() -> datetime:
    return datetime.now(UTC)


# --- Base (fonctions synchrones, appelées hors de la boucle d'événements) ------------------


def mark_interrupted(session: Session) -> int:
    """Exécutions restées `running` (service arrêté pendant leur exécution)."""
    count = session.execute(
        update(Run)
        .where(Run.status == RunStatus.RUNNING)
        .values(status=RunStatus.INTERRUPTED, finished_at=_now())
    ).rowcount
    session.commit()
    return count


# Verrou de transaction PostgreSQL : contrôle du budget et création de l'exécution d'un seul
# tenant à la fois (sinon deux lancements simultanés pourraient dépasser le budget)
BUDGET_LOCK = 0x41504C54


def new_run(
    factory: sessionmaker[Session],
    agent: AgentKind,
    ticket_id: int | None,
    trigger: RunTrigger,
    *,
    check_budget: bool,
) -> Run:
    """Crée l'exécution `running`. Lève `OverBudget` ou `AlreadyRunning` (index unique)."""
    with factory() as session:
        if check_budget:
            session.execute(select(func.pg_advisory_xact_lock(BUDGET_LOCK)))
            if not control.budget_allows(session):
                raise OverBudget
        run = Run(
            agent=agent,
            ticket_id=ticket_id,
            status=RunStatus.RUNNING,
            trigger=trigger,
            created_at=_now(),
        )
        session.add(run)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise AlreadyRunning from exc
        # Relue avant la fermeture : le commit a expiré l'objet, rendu hors de sa session
        session.refresh(run)
        return run


def finish(factory: sessionmaker[Session], run_id: uuid.UUID, result: AgentResult | None) -> None:
    """Enregistre le résultat ; `None` : exécution annulée (arrêt du service)."""
    with factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.finished_at = _now()
        if result is None:
            run.status = RunStatus.INTERRUPTED
            session.commit()
            return
        run.status = RunStatus.FAILED if result.error else RunStatus.SUCCEEDED
        run.simulation_date = result.simulation_date
        run.model = result.model
        run.output = result.output
        run.error = result.error
        run.total_cost_usd = (
            None if result.total_cost_usd is None else Decimal(str(result.total_cost_usd))
        )
        run.duration_ms = result.duration_ms
        run.num_turns = result.num_turns
        run.session_id = result.session_id
        run.available_tools = result.available_tools
        session.commit()


class Skip(StrEnum):
    """Motif pour lequel un ticket ouvert n'est pas lancé (affiché sur la page de pilotage)."""

    RUNNING = "running"
    HELD = "held"
    ABANDONED = "abandoned"
    HANDLED = "handled"
    PROPOSAL = "proposal"
    COOLDOWN = "cooldown"


@dataclass
class QueueSnapshot:
    """Dernier relevé de la file : tickets ouverts dans ShopSim, éligibles, et motifs des
    autres. La file n'est relevée que s'il reste une place libre, boucle en marche."""

    at: datetime
    open: int
    eligible: int
    skipped: dict[Skip, int]


@dataclass
class TicketHistory:
    """Ce que la plateforme sait déjà d'un ticket (schéma `agentpilot`)."""

    running: bool = False
    # Au moins une action mise en attente d'un humain, quel que soit son statut
    held: bool = False
    succeeded: int = 0
    # Exécutions réussies sous arrêt d'urgence (propositions seulement)
    proposals: int = 0
    # Échecs et interruptions
    attempts: int = 0
    last_failure: datetime | None = None

    def skip_reason(self, emergency: bool, now: datetime) -> Skip | None:
        """Pourquoi la boucle ne lance pas ce ticket, ou None s'il est éligible."""
        if self.running:
            return Skip.RUNNING
        if self.held:
            return Skip.HELD
        if self.attempts >= MAX_ATTEMPTS:
            return Skip.ABANDONED
        if self.succeeded > self.proposals:
            return Skip.HANDLED
        # Propositions seulement : relancé une fois, après la levée de l'arrêt d'urgence
        if self.proposals > 1:
            return Skip.HANDLED
        if self.proposals and emergency:
            return Skip.PROPOSAL
        if self.last_failure is not None and now - self.last_failure < RETRY_DELAY:
            return Skip.COOLDOWN
        return None


def histories(
    factory: sessionmaker[Session], ticket_ids: Iterable[int]
) -> dict[int, TicketHistory]:
    ids = list(ticket_ids)
    found: dict[int, TicketHistory] = {ticket_id: TicketHistory() for ticket_id in ids}
    with factory() as session:
        runs = session.execute(
            select(Run.ticket_id, Run.status, Run.propose_only, Run.finished_at).where(
                Run.agent == AgentKind.SUPPORT, Run.ticket_id.in_(ids)
            )
        )
        for ticket_id, status, propose_only, finished_at in runs:
            history = found[ticket_id]
            if status is RunStatus.RUNNING:
                history.running = True
            elif status is RunStatus.SUCCEEDED:
                history.succeeded += 1
                history.proposals += propose_only
            else:
                history.attempts += 1
                if status is RunStatus.FAILED and finished_at is not None:
                    history.last_failure = max(history.last_failure or finished_at, finished_at)
        held = session.scalars(
            select(PendingAction.ticket_id).where(PendingAction.ticket_id.in_(ids)).distinct()
        )
        for ticket_id in held:
            found[ticket_id].held = True
    return found


def abandoned(factory: sessionmaker[Session]) -> list[int]:
    """Tickets jamais traités après `MAX_ATTEMPTS` échecs ou interruptions."""
    failed = Run.status.in_([RunStatus.FAILED, RunStatus.INTERRUPTED])
    with factory() as session:
        return list(
            session.scalars(
                select(Run.ticket_id)
                .where(Run.agent == AgentKind.SUPPORT, Run.ticket_id.is_not(None))
                .group_by(Run.ticket_id)
                .having(
                    func.count().filter(failed) >= MAX_ATTEMPTS,
                    func.count().filter(Run.status == RunStatus.SUCCEEDED) == 0,
                )
                .order_by(Run.ticket_id)
            )
        )


def _describe(tool: str, params: dict[str, Any]) -> str:
    order, amount = params.get("order_id"), params.get("amount_cents")
    if tool == REFUND and isinstance(amount, int):
        return f"remboursement de {decisions.eur(amount)} sur la commande n°{order}"
    return {
        REPLY: "réponse envoyée au client",
        RESEND: f"renvoi de la commande n°{order}",
        ESCALATE: "ticket escaladé",
        RESOLVE: "ticket résolu",
    }.get(tool, tool)


def done_actions(factory: sessionmaker[Session], ticket_id: int) -> list[str]:
    """Actions réussies des exécutions inachevées du ticket (interrompues ou en échec) depuis
    sa dernière exécution réussie, d'après le journal.

    Une exécution réussie a rendu sa décision : ce qui la précède est clos. Relancer ensuite le
    ticket (à la main, ou après une remise à zéro de ShopSim) repart de zéro, sans liste de
    reprise ; les clés d'idempotence empêchent toujours une action en double."""
    ticket_runs = (Run.ticket_id == ticket_id, Run.agent == AgentKind.SUPPORT)
    with factory() as session:
        last_success = session.scalar(
            select(func.max(Run.created_at)).where(*ticket_runs, Run.status == RunStatus.SUCCEEDED)
        )
        query = (
            select(ToolCall.tool, ToolCall.input)
            .join(Run, Run.id == ToolCall.run_id)
            .where(
                *ticket_runs,
                ToolCall.outcome == CallOutcome.SUCCEEDED,
                ToolCall.tool.in_(ACTION_TOOL_NAMES),
            )
            .order_by(Run.created_at, ToolCall.seq)
        )
        if last_success is not None:
            query = query.where(Run.created_at > last_success)
        return [_describe(short_name(tool), params) for tool, params in session.execute(query)]


# --- Boucle --------------------------------------------------------------------------------


@dataclass
class Active:
    ticket_id: int | None
    trigger: RunTrigger
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)


class Worker:
    """Boucle de traitement et exécutions en cours du processus.

    `state` donne `session_factory` et `query` (l'état de l'application FastAPI) : relus à
    chaque usage, ils restent remplaçables par les tests.
    """

    def __init__(self, state: Any) -> None:  # noqa: ANN401
        self.state = state
        self.slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self.active: dict[uuid.UUID, Active] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.clock: Callable[[], datetime] = _now
        # Attente en cours (ShopSim en panne ou échecs en chaîne)
        self.backoff_level = 0
        self.waiting_until: datetime | None = None
        self.waiting_reason: str | None = None
        self._waiting_for_shopsim = False
        self.failures_in_row = 0
        self._tick_lock = asyncio.Lock()
        self._wake: asyncio.Event | None = None
        # Dernier relevé de la file (page de pilotage)
        self.queue: QueueSnapshot | None = None
        self._serve_scope: anyio.CancelScope | None = None
        # Modèle et version de la consigne de l'agent Support (l'évaluation peut en choisir
        # d'autres ; le service garde toujours ceux par défaut)
        self.model = sdk_runner.DEFAULT_MODEL
        self.prompt_version = DEFAULT_PROMPT

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.state.session_factory

    async def _db[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:  # noqa: ANN401
        return await asyncio.to_thread(function, self.factory, *args, **kwargs)

    def wake(self) -> None:
        """Tour anticipé : une place s'est libérée, ou un interrupteur a changé."""
        if self._wake is not None:
            self._wake.set()

    def running_tickets(self) -> list[int]:
        return sorted(a.ticket_id for a in self.active.values() if a.ticket_id is not None)

    # --- Exécutions ---

    async def execute(self, run: Run, launch: Launch) -> None:
        """Exécute une exécution déjà créée, dans une place libre, et enregistre le résultat.
        Tout ce qu'elle écrit dans les logs porte sa trace."""
        context = {"run_id": run.id} | ({"ticket_id": run.ticket_id} if run.ticket_id else {})
        with trace.bind(trace_id=run.trace_id, **context):
            await self._execute(run.id, run.ticket_id, run.trigger, run.agent, launch)

    async def _execute(
        self,
        run_id: uuid.UUID,
        ticket_id: int | None,
        trigger: RunTrigger,
        agent: AgentKind,
        launch: Launch,
    ) -> None:
        active = Active(ticket_id, trigger)
        self.active[run_id] = active
        result: AgentResult | None = None
        start = time.perf_counter()
        log_event(logger, "run.started", agent=agent, trigger=trigger)
        try:
            with active.scope:
                async with self.slots:
                    try:
                        result = await launch()
                    except Exception as exc:  # toute panne inattendue est enregistrée
                        log_event(logger, "run.crashed", logging.ERROR, exc_info=True)
                        result = AgentResult(
                            error=f"Erreur interne d'AgentPilot : {exc!r}",
                            error_type=sdk_runner.ErrorType.INTERNAL,
                        )
            await self._db(finish, run_id, result)
            if trigger is RunTrigger.LOOP and result is not None:
                self._count(result)
        finally:
            del self.active[run_id]
            self._log_finished(agent, trigger, result, start)
            self.wake()

    @staticmethod
    def _log_finished(
        agent: AgentKind, trigger: RunTrigger, result: AgentResult | None, start: float
    ) -> None:
        duration_s = time.perf_counter() - start
        metrics.run_finished(agent, trigger, result, duration_s)
        duration_ms = round(duration_s * 1000)
        if result is None:
            log_event(
                logger,
                "run.finished",
                logging.WARNING,
                status=RunStatus.INTERRUPTED,
                duration_ms=duration_ms,
            )
            return
        log_event(
            logger,
            "run.finished",
            logging.WARNING if result.error else logging.INFO,
            status=RunStatus.FAILED if result.error else RunStatus.SUCCEEDED,
            duration_ms=duration_ms,
            cost_usd=None if result.total_cost_usd is None else round(result.total_cost_usd, 6),
            num_turns=result.num_turns,
            # Jamais `result.error` : il peut citer la sortie de l'agent (texte du client)
            error_type=result.error_type,
        )

    def ticket_launch(self, run_id: uuid.UUID, ticket_id: int, done: list[str]) -> Launch:
        journal = DbJournal(self.factory, run_id)
        query, model, version = self.state.query, self.model, self.prompt_version
        return lambda: sdk_runner.decide_ticket(
            ticket_id, journal=journal, query=query, model=model, done=done, prompt_version=version
        )

    async def start_ticket(self, ticket_id: int, trigger: RunTrigger) -> tuple[Run, Launch]:
        """Crée l'exécution d'un ticket (budget et unicité vérifiés) et prépare son lancement.
        Lève `OverBudget` ou `AlreadyRunning`."""
        run = await self._db(new_run, AgentKind.SUPPORT, ticket_id, trigger, check_budget=True)
        done = await self._db(done_actions, ticket_id)
        return run, self.ticket_launch(run.id, ticket_id, done)

    def _count(self, result: AgentResult) -> None:
        if result.error is None:
            self.failures_in_row = 0
            self.backoff_level = 0
        else:
            self.failures_in_row += 1

    # --- Un tour ---

    def _backoff(self, reason: str, *, shopsim: bool = True) -> float:
        self._waiting_for_shopsim = shopsim
        self.backoff_level += 1
        delay = min(BACKOFF_BASE_S * 2 ** (self.backoff_level - 1), BACKOFF_MAX_S)
        self.waiting_until = self.clock() + timedelta(seconds=delay)
        self.waiting_reason = reason
        log_event(logger, "loop.backoff", logging.WARNING, delay_s=delay, reason=reason)
        return delay

    def _shopsim_ok(self) -> None:
        # Attente due aux échecs en chaîne : elle ne retombe qu'après une exécution réussie
        if self._waiting_for_shopsim:
            self.backoff_level = 0
            self._waiting_for_shopsim = False
        self.waiting_until = None
        self.waiting_reason = None

    async def _open_tickets(self) -> list[int]:
        ids: list[int] = []
        offset = 0
        while True:
            page = await get_json(
                "/tickets",
                "Liste des tickets introuvable.",
                {"status": "open", "limit": TICKETS_PAGE, "offset": offset},
            )
            ids += [ticket["id"] for ticket in page["items"]]
            offset += TICKETS_PAGE
            if offset >= page["total"] or not page["items"]:
                return ids

    async def tick(self) -> float:
        """Un tour de boucle ; renvoie le délai avant le suivant, en secondes."""
        async with self._tick_lock:
            # Requêtes du tour (santé de ShopSim, file, réessais) : une trace par tour
            with trace.bind(trace_id=trace.new_trace_id()):
                return await self._tick()

    async def _tick(self) -> float:
        if self.waiting_until is not None and self.clock() < self.waiting_until:
            return (self.waiting_until - self.clock()).total_seconds()
        settings = await self._db(control.load)
        try:
            await get_json("/clock", "Horloge de ShopSim introuvable.")
            if not settings.emergency_stop:
                for action_id in await self._db(decisions.to_retry):
                    if await decisions.retry(self.factory, action_id) is Notice.UNAVAILABLE:
                        return self._backoff("ShopSim en panne pendant le réessai d'une action.")
            if settings.loop_paused:
                self._shopsim_ok()
                return interval_s()
            if self.failures_in_row >= MAX_FAILURES_IN_ROW:
                self.failures_in_row = 0
                return self._backoff(
                    f"{MAX_FAILURES_IN_ROW} exécutions en échec d'affilée.", shopsim=False
                )
            free = MAX_CONCURRENT_RUNS - len(self.active)
            open_ids = await self._open_tickets() if free > 0 else []
        except ShopSimUnavailable as exc:
            return self._backoff(str(exc))
        self._shopsim_ok()
        if free > 0:
            await self._launch_eligible(open_ids, free, settings.emergency_stop)
        return interval_s()

    async def _launch_eligible(self, open_ids: list[int], free: int, emergency: bool) -> None:
        found = await self._db(histories, open_ids)
        running = set(self.running_tickets())
        now = self.clock()
        eligible: list[int] = []
        skipped: dict[Skip, int] = {}
        for ticket_id in open_ids:
            reason = Skip.RUNNING if ticket_id in running else None
            reason = reason or found[ticket_id].skip_reason(emergency, now)
            if reason is None:
                eligible.append(ticket_id)
            else:
                skipped[reason] = skipped.get(reason, 0) + 1
        self.queue = QueueSnapshot(now, len(open_ids), len(eligible), skipped)
        for ticket_id in eligible:
            if free == 0:
                return
            try:
                run, launch = await self.start_ticket(ticket_id, RunTrigger.LOOP)
            except AlreadyRunning:
                continue
            except OverBudget:
                await self._db(control.pause, None, PauseReason.BUDGET)
                return
            task = asyncio.create_task(self.execute(run, launch))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            free -= 1

    # --- Service ---

    async def serve(self) -> None:
        """Boucle de fond, jusqu'à `shutdown`."""
        self._wake = asyncio.Event()
        with anyio.CancelScope() as scope:
            self._serve_scope = scope
            while True:
                try:
                    delay = await self.tick()
                except Exception:  # la boucle ne s'arrête jamais sur une erreur
                    log_event(logger, "loop.error", logging.ERROR, exc_info=True)
                    delay = interval_s()
                with anyio.move_on_after(delay):
                    await self._wake.wait()
                self._wake.clear()

    async def drain(self) -> None:
        """Attend la fin des exécutions lancées par la boucle (tests)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks))

    async def shutdown(self, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
        """Arrête la boucle et annule les exécutions en cours (CLI arrêtée, `interrupted`)."""
        if self._serve_scope is not None:
            self._serve_scope.cancel()
        for active in list(self.active.values()):
            active.scope.cancel()
        with anyio.move_on_after(timeout_s):
            while self.active:
                await anyio.sleep(0.05)
