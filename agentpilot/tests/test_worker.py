"""Boucle de fond, reprise, arrêt d'urgence, budget et pannes de ShopSim, sans appel à Claude.

ShopSim de test servie en HTTP (vrais tickets ouverts, vrais remboursements). La base
d'AgentPilot est écrite par les sessions du service (vrais commits, une connexion par
session) : les exécutions tournent en parallèle ; les tables sont vidées après chaque test.
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import control, decisions
from agentpilot.approval import REFUND
from agentpilot.db import SessionLocal
from agentpilot.hooks import EMERGENCY_MESSAGE, SupportHooks
from agentpilot.journal import DbJournal
from agentpilot.models import (
    AgentKind,
    CallOutcome,
    PauseReason,
    PendingAction,
    PendingStatus,
    Run,
    RunStatus,
    RunTrigger,
    ToolCall,
)
from agentpilot.support_agent import ACTION_TOOL_NAMES, READ_TOOL_NAMES
from agentpilot.tests.conftest import (
    DECISION,
    HookDrivingQuery,
    MemoryJournal,
    Shop,
    init_message,
    refund,
    reset_agentpilot,
    result_message,
)
from agentpilot.worker import (
    BACKOFF_MAX_S,
    MAX_ATTEMPTS,
    MAX_CONCURRENT_RUNS,
    RETRY_DELAY,
    AlreadyRunning,
    QueueSnapshot,
    Skip,
    Worker,
    mark_interrupted,
    new_run,
)
from mcp_server import idempotency
from mcp_server.shopsim_client import post_json
from shopsim.app.models import Payment, Ticket, TicketStatus

# Aucun service n'écoute sur le port 9 (discard) : ShopSim injoignable
DOWN_URL = "http://127.0.0.1:9"


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def live(
    agentpilot_engine: Engine, session_factory: sessionmaker[Session]
) -> sessionmaker[Session]:
    """Les sessions du service, boucle en marche au départ."""
    reset_agentpilot(agentpilot_engine, loop_paused=False)
    return session_factory


class TicketQuery:
    """Faux SDK : décide le ticket de la consigne ; peut bloquer ou échouer, et compte les
    exécutions simultanées."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.gate = asyncio.Event()
        self.gate.set()
        self.prompts: list[str] = []
        self.running = 0
        self.max_running = 0

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompts.append(prompt)
        return self._stream(prompt)

    async def _stream(self, prompt: str) -> AsyncIterator[Any]:
        match = re.search(r"ticket n°(\d+)", prompt)
        assert match is not None
        ticket_id = int(match.group(1))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            yield init_message()
            await self.gate.wait()
            if self.fail:
                yield result_message(None, is_error=True, subtype="error_during_execution")
            else:
                yield result_message(DECISION | {"ticket_id": ticket_id})
        finally:
            self.running -= 1

    def tickets(self) -> list[int]:
        return [int(re.findall(r"n°(\d+)", p)[0]) for p in self.prompts]


@pytest.fixture
def query() -> TicketQuery:
    return TicketQuery()


@pytest.fixture
def worker(
    live: sessionmaker[Session],
    query: TicketQuery,
    shopsim: str,
    sim_clock: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Worker]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv(control.BUDGET_ENV, "100")
    yield Worker(SimpleNamespace(session_factory=live, query=query))


def tickets(shop: Shop, count: int, status: TicketStatus = TicketStatus.OPEN) -> list[Ticket]:
    found = []
    for _ in range(count):
        payment = shop.payment(4_000)
        found.append(shop.make_ticket(payment.order.customer, payment.order, status=status))
    return found


def runs(factory: sessionmaker[Session], ticket: Ticket) -> list[Run]:
    with factory() as session:
        return list(
            session.scalars(select(Run).where(Run.ticket_id == ticket.id).order_by(Run.created_at))
        )


def add_run(
    factory: sessionmaker[Session],
    ticket_id: int,
    status: RunStatus,
    *,
    cost: str | None = None,
    propose_only: bool = False,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
    started: bool = True,
) -> uuid.UUID:
    now = datetime.now(UTC)
    with factory() as session:
        run = Run(
            agent=AgentKind.SUPPORT,
            ticket_id=ticket_id,
            status=status,
            trigger=RunTrigger.LOOP,
            propose_only=propose_only,
            total_cost_usd=None if cost is None else Decimal(cost),
            model="claude-sonnet-5-5" if started else None,
            created_at=created_at or now,
            finished_at=None if status is RunStatus.RUNNING else (finished_at or now),
        )
        session.add(run)
        session.commit()
        return run.id


def add_action(
    factory: sessionmaker[Session],
    ticket: Ticket,
    payment: Payment,
    status: PendingStatus,
) -> uuid.UUID:
    run_id = add_run(factory, ticket.id, RunStatus.SUCCEEDED)
    with factory() as session:
        action = PendingAction(
            run_id=run_id,
            ticket_id=ticket.id,
            tool=REFUND,
            params={
                "ticket_id": ticket.id,
                "order_id": payment.order_id,
                "amount_cents": payment.amount_cents,
                "reason": "Retour",
            },
            reasons=["over_threshold"],
            facts={"value_cents": payment.amount_cents},
            idempotency_key=idempotency.refund_key(ticket.id, payment.order_id),
            status=status,
            created_at=datetime.now(UTC),
            decided_by="Alice" if status is not PendingStatus.PENDING else None,
            decided_at=datetime.now(UTC) if status is not PendingStatus.PENDING else None,
        )
        session.add(action)
        session.commit()
        return action.id


async def started(query: TicketQuery, count: int) -> None:
    async with asyncio.timeout(5):
        while query.running < count:
            await asyncio.sleep(0.01)


# --- Sessions du service --------------------------------------------------------------


def test_tests_use_the_service_session_configuration(live: sessionmaker[Session]) -> None:
    assert live.kw == SessionLocal.kw | {"bind": live.kw["bind"]}
    assert live.kw["expire_on_commit"] is True


@pytest.mark.anyio
async def test_first_loop_launch_reads_the_new_run_outside_its_session(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    """Régression (premier lancement réel) : `new_run` rendait une exécution expirée par son
    commit après avoir fermé sa session ; lire `run.id` levait `DetachedInstanceError`."""
    run = new_run(live, AgentKind.SUPPORT, 4242, RunTrigger.LOOP, check_budget=False)
    assert run.id is not None and run.status is RunStatus.RUNNING

    [ticket] = tickets(shop, 1)
    await worker.tick()
    await worker.drain()

    assert query.tickets() == [ticket.id]
    assert [r.status for r in runs(live, ticket)] == [RunStatus.SUCCEEDED]


# --- Pas de double traitement ------------------------------------------------------------


@pytest.mark.anyio
async def test_open_tickets_are_processed_two_at_a_time(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    first, second, third = tickets(shop, 3)
    query.gate.clear()

    await worker.tick()
    await started(query, MAX_CONCURRENT_RUNS)
    await worker.tick()  # aucune place libre : rien de plus
    assert query.tickets() == [first.id, second.id]
    assert worker.running_tickets() == [first.id, second.id]

    query.gate.set()
    await worker.drain()
    await worker.tick()
    await worker.drain()
    await worker.tick()  # tout est traité : plus rien à lancer
    await worker.drain()

    assert query.tickets() == [first.id, second.id, third.id]
    assert query.max_running == MAX_CONCURRENT_RUNS
    for ticket in (first, second, third):
        [done] = runs(live, ticket)
        assert done.status is RunStatus.SUCCEEDED and done.trigger is RunTrigger.LOOP


@pytest.mark.anyio
async def test_simultaneous_loops_never_process_a_ticket_twice(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    """Deux boucles (comme deux processus) sur les mêmes tickets : l'index en base tranche."""
    pair = tickets(shop, 2)
    other = Worker(worker.state)
    query.gate.clear()

    await asyncio.gather(worker.tick(), other.tick(), worker.tick())
    await started(query, 2)
    query.gate.set()
    await asyncio.gather(worker.drain(), other.drain())

    assert sorted(query.tickets()) == [t.id for t in pair]
    for ticket in pair:
        assert len(runs(live, ticket)) == 1


def test_database_refuses_a_second_running_run(live: sessionmaker[Session]) -> None:
    new_run(live, AgentKind.SUPPORT, 4242, RunTrigger.LOOP, check_budget=False)

    with pytest.raises(AlreadyRunning):
        new_run(live, AgentKind.SUPPORT, 4242, RunTrigger.MANUAL, check_budget=False)
    # Autre ticket, ou exécution terminée : rien n'empêche
    new_run(live, AgentKind.SUPPORT, 4243, RunTrigger.LOOP, check_budget=False)
    with live() as session:
        session.execute(update(Run).where(Run.ticket_id == 4242).values(status=RunStatus.FAILED))
        session.commit()
    new_run(live, AgentKind.SUPPORT, 4242, RunTrigger.LOOP, check_budget=False)


def test_manual_run_of_a_running_ticket_is_refused(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    new_run(session_factory, AgentKind.SUPPORT, 4242, RunTrigger.LOOP, check_budget=False)

    response = client.post("/runs/support/4242")

    assert response.status_code == 409


@pytest.mark.anyio
async def test_handled_tickets_are_not_relaunched(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    done, held, rejected, failing, recent, escalated = tickets(shop, 6)
    shop.session.execute(
        update(Ticket).where(Ticket.id == escalated.id).values(status=TicketStatus.ESCALATED)
    )
    shop.session.flush()
    add_run(live, done.id, RunStatus.SUCCEEDED)
    add_action(live, held, shop.payment(9_000), PendingStatus.PENDING)
    add_action(live, rejected, shop.payment(9_000), PendingStatus.REJECTED)
    long_ago = datetime.now(UTC) - RETRY_DELAY * 2
    for _ in range(MAX_ATTEMPTS):
        add_run(live, failing.id, RunStatus.FAILED, finished_at=long_ago)
    add_run(live, recent.id, RunStatus.FAILED)

    await worker.tick()
    await worker.drain()

    assert query.tickets() == []
    # L'escaladé n'est plus ouvert ; les autres sont écartés, chacun avec son motif
    assert worker.queue is not None
    assert (worker.queue.open, worker.queue.eligible) == (5, 0)
    assert worker.queue.skipped == {
        Skip.HANDLED: 1,
        Skip.HELD: 2,
        Skip.ABANDONED: 1,
        Skip.COOLDOWN: 1,
    }
    worker.clock = lambda: datetime.now(UTC) + RETRY_DELAY  # délai écoulé
    await worker.tick()
    await worker.drain()
    assert query.tickets() == [recent.id]


# --- Reprise -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_interrupted_run_is_relaunched_once_with_what_was_done(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    [ticket] = tickets(shop, 1)
    run_id = add_run(live, ticket.id, RunStatus.RUNNING)
    with live() as session:
        for seq, (name, arguments) in enumerate(
            [
                ("reply_to_ticket", {"ticket_id": ticket.id, "body": "Bonjour"}),
                ("refund_order", {"ticket_id": ticket.id, "order_id": 7, "amount_cents": 3_000}),
            ],
            start=1,
        ):
            session.add(
                ToolCall(
                    run_id=run_id,
                    seq=seq,
                    tool=f"mcp__shopsim__{name}",
                    input=arguments,
                    outcome=CallOutcome.SUCCEEDED,
                    created_at=datetime.now(UTC),
                )
            )
        session.commit()
        # Redémarrage du service
        assert mark_interrupted(session) == 1

    await worker.tick()
    await worker.drain()
    await worker.tick()
    await worker.drain()

    [prompt] = query.prompts
    assert "Reprise" in prompt
    assert "réponse envoyée au client" in prompt
    assert "remboursement de 30,00 € sur la commande n°7" in prompt
    assert [r.status for r in runs(live, ticket)] == [RunStatus.INTERRUPTED, RunStatus.SUCCEEDED]


@pytest.mark.anyio
async def test_relaunch_does_not_refund_twice(
    worker: Worker, shop: Shop, live: sessionmaker[Session]
) -> None:
    """L'exécution interrompue avait remboursé ; la relance refait le même appel : même clé,
    ShopSim reconnaît le rejeu, un seul remboursement."""
    payment = shop.payment(4_000)
    ticket = shop.ticket(payment)
    worker.state.query = HookDrivingQuery(
        refund(ticket, payment, 3_000), output=DECISION | {"ticket_id": ticket.id}
    )
    await worker.tick()
    await worker.drain()
    with live() as session:
        session.execute(
            update(Run).where(Run.ticket_id == ticket.id).values(status=RunStatus.INTERRUPTED)
        )
        session.commit()

    await worker.tick()
    await worker.drain()

    assert shop.refunds(payment) == 1
    first, second = runs(live, ticket)
    assert second.status is RunStatus.SUCCEEDED
    with live() as session:
        keys = session.scalars(
            select(ToolCall.response["idempotency_key"].astext)
            .where(ToolCall.run_id.in_([first.id, second.id]))
            .order_by(ToolCall.created_at)
        ).all()
    assert len(set(keys)) == 1


@pytest.mark.anyio
async def test_approved_action_already_done_is_not_done_again(
    worker: Worker, shop: Shop, live: sessionmaker[Session]
) -> None:
    """Le service s'est arrêté juste après l'appel à ShopSim, avant d'enregistrer le résultat."""
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    action_id = add_action(live, ticket, payment, PendingStatus.APPROVED)
    await post_json(
        "/refunds",
        "introuvable",
        {"payment_id": payment.id, "amount_cents": 12_000, "reason": "Retour"},
        idempotency.refund_key(ticket.id, payment.order_id),
    )
    control.pause(live, "Alice", PauseReason.MANUAL)

    await worker.tick()  # boucle en pause : le réessai a lieu quand même

    assert shop.refunds(payment) == 1
    assert decisions.load(live, action_id).status is PendingStatus.EXECUTED  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_approved_action_is_executed_once_at_restart(
    worker: Worker, shop: Shop, live: sessionmaker[Session]
) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    action_id = add_action(live, ticket, payment, PendingStatus.APPROVED)

    await worker.tick()
    await worker.tick()

    assert shop.refunds(payment) == 1
    assert decisions.load(live, action_id).status is PendingStatus.EXECUTED  # type: ignore[union-attr]


# --- Arrêt d'urgence ---------------------------------------------------------------------


def hook_data(name: str, **tool_input: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input}


def denial(output: Any) -> str | None:  # noqa: ANN401
    return output.get("hookSpecificOutput", {}).get("permissionDecisionReason")


@pytest.mark.anyio
async def test_emergency_stop_denies_every_action_but_not_reads() -> None:
    journal = MemoryJournal(stopped=True)
    hooks = SupportHooks(1, journal)

    for name in sorted(ACTION_TOOL_NAMES):
        output = await hooks.pre_tool_use(hook_data(name, ticket_id=1), "t", {"signal": None})
        assert denial(output) == EMERGENCY_MESSAGE, name
    for name in sorted(READ_TOOL_NAMES):
        assert await hooks.pre_tool_use(hook_data(name), "t", {"signal": None}) == {}
    assert journal.propose_only
    assert {c.outcome for c in journal.calls} == {CallOutcome.DENIED}


@pytest.mark.anyio
async def test_emergency_stop_applies_during_a_run() -> None:
    journal = MemoryJournal()
    hooks = SupportHooks(1, journal)
    reply = hook_data("mcp__shopsim__reply_to_ticket", ticket_id=1, body="Bonjour")

    assert await hooks.pre_tool_use(reply, "t1", {"signal": None}) == {}
    journal.stopped = True
    assert denial(await hooks.pre_tool_use(reply, "t2", {"signal": None})) == EMERGENCY_MESSAGE


class BrokenJournal(MemoryJournal):
    def emergency_stop(self) -> bool:
        raise ConnectionError("base injoignable")


@pytest.mark.anyio
async def test_unreadable_emergency_switch_denies_the_action() -> None:
    hooks = SupportHooks(1, BrokenJournal())
    reply = hook_data("mcp__shopsim__reply_to_ticket", ticket_id=1, body="Bonjour")

    assert denial(await hooks.pre_tool_use(reply, "t", {"signal": None})) is not None


def test_emergency_switch_is_read_from_the_database(live: sessionmaker[Session]) -> None:
    run_id = add_run(live, 1, RunStatus.RUNNING)
    journal = DbJournal(live, run_id)
    assert journal.emergency_stop() is False

    control.set_emergency(live, True, "Alice")
    journal.mark_propose_only()

    assert journal.emergency_stop() is True
    settings = control.load(live)
    assert settings.emergency_by == "Alice" and settings.emergency_at is not None
    with live() as session:
        assert session.get_one(Run, run_id).propose_only


@pytest.mark.anyio
async def test_no_automatic_retry_during_emergency_stop(
    worker: Worker, shop: Shop, live: sessionmaker[Session]
) -> None:
    payment = shop.payment(12_000)
    action_id = add_action(live, shop.ticket(payment), payment, PendingStatus.APPROVED)
    control.set_emergency(live, True, "Alice")

    await worker.tick()

    assert shop.refunds(payment) == 0
    assert decisions.load(live, action_id).status is PendingStatus.APPROVED  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_proposal_is_relaunched_once_after_the_stop(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    once, twice = tickets(shop, 2)
    add_run(live, once.id, RunStatus.SUCCEEDED, propose_only=True)
    for _ in range(2):
        add_run(live, twice.id, RunStatus.SUCCEEDED, propose_only=True)
    control.set_emergency(live, True, "Alice")

    await worker.tick()
    await worker.drain()
    assert query.tickets() == []

    control.set_emergency(live, False, "Alice")
    for _ in range(2):
        await worker.tick()
        await worker.drain()
    assert query.tickets() == [once.id]


def test_human_approval_still_works_during_emergency_stop(
    client: TestClient, shop: Shop, session_factory: sessionmaker[Session]
) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    run_id = add_run(session_factory, ticket.id, RunStatus.SUCCEEDED)
    with session_factory() as session:
        session.execute(update(Run).where(Run.id == run_id).values(output=DECISION))
        session.commit()
    action_id = add_action(session_factory, ticket, payment, PendingStatus.PENDING)
    control.set_emergency(session_factory, True, "Alice")

    page = client.get(f"/approvals/{action_id}")
    approved = client.post(
        f"/approvals/{action_id}/approve", data={"name": "Bob"}, follow_redirects=False
    )

    assert "Arrêt d'urgence actif" in page.text
    assert approved.status_code == 303 and "notice=executed" in approved.headers["location"]
    assert shop.refunds(payment) == 1


# --- Budget ------------------------------------------------------------------------------


def test_spent_today_counts_unknown_costs_at_the_run_cap(live: sessionmaker[Session]) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=1)
    add_run(live, 1, RunStatus.SUCCEEDED, cost="1.20")
    add_run(live, 2, RunStatus.INTERRUPTED)  # coût inconnu : 0,50
    add_run(live, 3, RunStatus.FAILED, started=False)  # échec avant l'agent : 0
    add_run(live, 4, RunStatus.SUCCEEDED, cost="3", created_at=yesterday)

    with live() as session:
        assert control.spent_today(session) == Decimal("1.70")


@pytest.mark.anyio
async def test_budget_reached_pauses_the_loop(
    worker: Worker,
    query: TicketQuery,
    shop: Shop,
    live: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets(shop, 2)
    monkeypatch.setenv(control.BUDGET_ENV, "2")
    add_run(live, 1, RunStatus.SUCCEEDED, cost="1.60")

    await worker.tick()
    await worker.drain()

    assert query.tickets() == []
    settings = control.load(live)
    assert settings.loop_paused and settings.pause_reason is PauseReason.BUDGET
    assert control.resume(live, "Alice") is False
    assert control.load(live).loop_paused


@pytest.mark.anyio
async def test_budget_is_never_exceeded_by_running_runs(
    worker: Worker,
    query: TicketQuery,
    shop: Shop,
    live: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deux places libres, mais le budget n'en couvre qu'une (exécution en cours = 0,50)."""
    tickets(shop, 2)
    monkeypatch.setenv(control.BUDGET_ENV, "0.9")
    query.gate.clear()

    await worker.tick()
    await started(query, 1)
    query.gate.set()
    await worker.drain()

    assert len(query.prompts) == 1
    assert control.load(live).pause_reason is PauseReason.BUDGET


def test_manual_run_over_budget_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(control.BUDGET_ENV, "0.1")

    assert client.post("/runs/support/4242").status_code == 429


# --- ShopSim en panne --------------------------------------------------------------------


@pytest.mark.anyio
async def test_shopsim_down_makes_the_loop_wait_longer_and_longer(
    worker: Worker,
    query: TicketQuery,
    shop: Shop,
    live: sessionmaker[Session],
    shopsim: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets(shop, 1)
    payment = shop.payment(12_000)
    action_id = add_action(live, shop.ticket(payment), payment, PendingStatus.APPROVED)
    monkeypatch.setenv("SHOPSIM_URL", DOWN_URL)
    now = datetime.now(UTC)
    worker.clock = lambda: now

    delays = [await worker.tick()]
    assert 0 < await worker.tick() <= delays[0]  # encore en attente : rien n'est tenté
    for _ in range(8):
        now += timedelta(seconds=BACKOFF_MAX_S)
        delays.append(await worker.tick())

    assert delays[:4] == [10, 20, 40, 80]
    assert delays[-1] == BACKOFF_MAX_S
    assert worker.waiting_reason is not None and "injoignable" in worker.waiting_reason
    assert query.prompts == []
    with live() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1  # celle de l'action
    assert decisions.load(live, action_id).status is PendingStatus.APPROVED  # type: ignore[union-attr]

    # Rétablissement : tout reprend et l'attente retombe
    monkeypatch.setenv("SHOPSIM_URL", shopsim)
    now += timedelta(seconds=BACKOFF_MAX_S)
    await worker.tick()
    await worker.drain()
    assert worker.waiting_until is None and worker.backoff_level == 0
    assert len(query.prompts) == 1
    assert shop.refunds(payment) == 1


@pytest.mark.anyio
async def test_failures_in_a_row_make_the_loop_wait(
    worker: Worker, query: TicketQuery, shop: Shop
) -> None:
    *failing, last = tickets(shop, 5)
    query.fail = True

    for _ in range(2):
        await worker.tick()
        await worker.drain()
    delay = await worker.tick()
    await worker.drain()

    assert query.tickets() == [t.id for t in failing]
    assert delay == 10 and "échec" in (worker.waiting_reason or "")


# --- Pause, page de pilotage -------------------------------------------------------------


@pytest.mark.anyio
async def test_pause_is_kept_in_the_database(
    worker: Worker, query: TicketQuery, shop: Shop, live: sessionmaker[Session]
) -> None:
    tickets(shop, 1)
    control.pause(live, "Alice", PauseReason.MANUAL)

    await worker.tick()
    # Redémarrage : une autre instance retrouve la pause
    await Worker(worker.state).tick()
    assert query.prompts == []

    assert control.resume(live, "Alice")
    await worker.tick()
    await worker.drain()
    assert len(query.prompts) == 1


def test_control_page_switches(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    def post(path: str, **form: str) -> Any:  # noqa: ANN401
        return client.post(path, data=form, follow_redirects=False)

    assert post("/control/loop/pause", name=" ").status_code == 422
    assert post("/control/loop/pause", name="Alice").status_code == 303
    assert control.load(session_factory).loop_paused
    assert post("/control/loop/resume", name="Alice").headers["location"].endswith("resumed")
    assert not control.load(session_factory).loop_paused
    assert post("/control/emergency", on="true", name="<b>Bob</b>").status_code == 303

    page = client.get("/control")
    status = client.get("/control/status").json()

    assert page.status_code == 200
    assert "&lt;b&gt;Bob&lt;/b&gt;" in page.text and "<b>Bob</b>" not in page.text
    assert "Arrêt d'urgence actif" in page.text
    assert status["emergency_stop"] is True and status["emergency_by"] == "<b>Bob</b>"
    assert status["loop_paused"] is False and status["max_concurrent_runs"] == 2


@pytest.mark.anyio
async def test_serve_stops_on_shutdown(worker: Worker, live: sessionmaker[Session]) -> None:
    control.pause(live, "Alice", PauseReason.MANUAL)
    serving = asyncio.create_task(worker.serve())
    await asyncio.sleep(0.2)

    await worker.shutdown(timeout_s=5)

    async with asyncio.timeout(5):
        await serving


def test_control_page_explains_an_empty_queue(client: TestClient) -> None:
    """Boucle en marche, places libres, mais rien à lancer : la page dit pourquoi."""
    worker = client.app.state.worker  # type: ignore[attr-defined]
    worker.queue = QueueSnapshot(
        datetime.now(UTC), open=12, eligible=0, skipped={Skip.HANDLED: 8, Skip.HELD: 4}
    )
    try:
        page = client.get("/control").text
        status = client.get("/control/status").json()
    finally:
        worker.queue = None

    assert "12 ouverts dans ShopSim" in page and "0 à traiter" in page
    assert "8 déjà traités" in page and "4 avec une action confiée à un humain" in page
    assert "Rien à lancer pour l'instant" in page
    assert status["open_tickets"] == 12 and status["eligible_tickets"] == 0
    assert status["skipped_tickets"] == {"handled": 8, "held": 4}
