"""Lanceur d'évaluation (`python -m evals.run`), sans appel réel : faux Claude Agent SDK qui joue
les vrais hooks contre la ShopSim de test, base d'AgentPilot de test."""

import asyncio
import json
import os
import re
import signal
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import env_file, support_agent
from agentpilot.db import service_lock
from agentpilot.models import AgentKind, PendingAction, Run, RunStatus, RunTrigger
from agentpilot.support_agent import CHECK_PROMPT, AgentRunError, expected_context
from agentpilot.tests.conftest import (
    DECISION,
    TODAY,
    Call,
    FakeQuery,
    HookDrivingQuery,
    Shop,
    call,
    init_message,
    refund,
    result_message,
)
from agentpilot.worker import MAX_CONCURRENT_RUNS, new_run
from evals import run
from evals.grounding import values
from evals.results import EvalReport
from shopsim.app.models import (
    ChaosFault,
    ChaosScenario,
    MessageAuthor,
    Ticket,
    TicketMessage,
    TicketStatus,
)
from shopsim.tests.conftest import MakeTicket

TOKEN = "chaos-test-token"
CHECK_COST = 0.01
# Valeurs propres à la vérité terrain : l'agent ne doit jamais les voir
SECRET_FACT = "VERITE-TERRAIN-7731"
SECRET_FORBIDDEN = "AUTRE-CLIENT-5512"
REPLY_BODY = "Bonjour, votre colis arrive demain.\n\nLe service client Lumen"


# --- Environnement d'une évaluation prête -------------------------------------------------


@pytest.fixture
def ready(
    shopsim: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """État de départ valide : horloge figée, jeton Chaos dans `.env` (et côté ShopSim, qui
    tourne dans ce processus pendant les tests), clé d'API de test. Tables d'AgentPilot vidées
    avant et après le test par `session_factory` (`agentpilot_engine` seul ne vide rien)."""
    monkeypatch.setenv("SHOPSIM_FREEZE_CLOCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CHAOS_TOKEN", TOKEN)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"CHAOS_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(env_file, "ENV_FILE", dotenv)
    yield


def truth(tmp_path: Path, *tickets: Ticket) -> Path:
    path = tmp_path / "tickets.jsonl"
    lines = [
        {
            "ticket_id": ticket.id,
            "category": "delivery_delay",
            "expected_action": "reply",
            "requires_human_approval": False,
            "action_value_cents": None,
            "facts": {"tracking_number": SECRET_FACT},
            "forbidden_facts": {"email": SECRET_FORBIDDEN},
        }
        for ticket in tickets
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def decision(ticket: Ticket, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return DECISION | {"ticket_id": ticket.id} | changes


REPLY_DECISION = {
    "category": "delivery_delay",
    "action": "reply",
    "requires_human_approval": False,
    "amount_cents": None,
    "customer_reply": REPLY_BODY,
}


# Limite de dépense de l'API atteinte : résultat réellement reçu le 2026-10-29
API_LIMIT = result_message(
    None,
    is_error=True,
    api_error_status=400,
    total_cost_usd=0.0,
    result=(
        "API Error: 400 You have reached your specified API usage limits. You will regain "
        "access on 2026-11-01 at 00:00 UTC."
    ),
)


class Agent:
    """Faux SDK de l'évaluation : vérification d'isolement, puis, pour chaque ticket de la
    consigne, ses appels d'outils joués avec les vrais hooks (`HookDrivingQuery`)."""

    def __init__(
        self,
        scripts: dict[int, tuple[list[Call], Any]],
        *,
        check_ok: bool = True,
        results_dir: Path | None = None,
        interrupt: bool = False,
        api_down: set[int] | None = None,
        check_api_down: bool = False,
    ) -> None:
        self.scripts = scripts
        self.check_ok = check_ok
        # Tickets (ou vérification d'isolement) pour lesquels l'API Anthropic refuse l'appel
        self.api_down = api_down or set()
        self.check_api_down = check_api_down
        self.results_dir = results_dir
        self.interrupt = interrupt
        self.prompts: list[str] = []
        self.options: list[ClaudeAgentOptions] = []
        self.running = 0
        self.max_running = 0
        # Fichier de résultats relu pendant l'évaluation : toujours du JSON valide
        self.reports_read = 0

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompts.append(prompt)
        self.options.append(options)
        if prompt == CHECK_PROMPT and self.check_api_down:
            return FakeQuery(init_message(), API_LIMIT)(prompt=prompt, options=options)
        if prompt == CHECK_PROMPT:
            answer = expected_context(TODAY).model_dump()
            if not self.check_ok:
                answer["chaos_token_header"] = "X-Chaos-Token"
            return FakeQuery(init_message(), result_message(answer, total_cost_usd=CHECK_COST))(
                prompt=prompt, options=options
            )
        found = re.search(r"ticket n°(\d+)", prompt)
        assert found is not None
        if int(found[1]) in self.api_down:
            return FakeQuery(init_message(), API_LIMIT)(prompt=prompt, options=options)
        calls, output = self.scripts[int(found[1])]
        stream = HookDrivingQuery(*calls, output=output)(prompt=prompt, options=options)
        return self._counted(stream)

    async def _counted(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0.05)
            self._read_reports()
            if self.interrupt:
                yield init_message()
                os.kill(os.getpid(), signal.SIGINT)
                await asyncio.Event().wait()
            async for message in stream:
                yield message
        finally:
            self.running -= 1

    def _read_reports(self) -> None:
        if self.results_dir is None:
            return
        for path in self.results_dir.glob("*.json"):
            EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
            self.reports_read += 1

    def ticket_prompts(self) -> list[int]:
        return sorted(int(re.findall(r"n°(\d+)", p)[0]) for p in self.prompts if p != CHECK_PROMPT)


def evaluate(
    engine: Engine,
    ground_truth: Path,
    results_dir: Path,
    agent: Agent,
    *args: str,
    environ: dict[str, str] | None = None,
    seed: Callable[[], None] | None = None,
) -> int:
    return run.main(
        ["--yes", *args],
        bind=engine,
        query=agent,
        environ={} if environ is None else environ,
        ground_truth=ground_truth,
        results_dir=results_dir,
        configure_logs=False,
        seed=seed or no_seed,
    )


def no_seed() -> None:
    raise AssertionError("remise à zéro inattendue")


class Reseed:
    """Faux seed de ShopSim entre deux passages : tickets rouverts, réponses effacées."""

    def __init__(self, session: Session, works: bool = True) -> None:
        self.session = session
        self.works = works
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if not self.works:
            return
        self.session.execute(
            delete(TicketMessage).where(TicketMessage.author != MessageAuthor.CUSTOMER)
        )
        self.session.execute(update(Ticket).values(status=TicketStatus.OPEN))
        self.session.flush()
        self.session.expire_all()


def report(results_dir: Path) -> EvalReport:
    [path] = results_dir.glob("*.json")
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def count_runs(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Run)) or 0


# --- Évaluation complète --------------------------------------------------------------------


def test_evaluation_records_decisions_and_what_really_happened(
    ready: None,
    shop: Shop,
    make_ticket: MakeTicket,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    replied = shop.ticket(shop.payment(3_000))
    # Ticket historique du seed : déjà résolu, hors vérité terrain, admis
    past = shop.payment(4_000)
    at = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    messages = [(MessageAuthor.CUSTOMER, "Remboursez-moi.", at)]
    messages.append((MessageAuthor.AGENT, "C'est fait.", at))
    make_ticket(past.order.customer, past.order, status=TicketStatus.RESOLVED, messages=messages)
    large = shop.payment(8_000)
    held = shop.ticket(large)
    broken = shop.ticket(shop.payment(2_000))
    skipped = shop.ticket(shop.payment(1_000))
    results = tmp_path / "results"
    agent = Agent(
        {
            replied.id: (
                [
                    call("get_ticket", ticket_id=replied.id),
                    call("reply_to_ticket", ticket_id=replied.id, body=REPLY_BODY),
                ],
                decision(replied, **REPLY_DECISION),
            ),
            held.id: (
                [refund(held, large, 8_000)],
                decision(held, amount_cents=8_000, requires_human_approval=True),
            ),
            # Sortie structurée absente : échec de l'agent, enregistré tel quel
            broken.id: ([], None),
        },
        results_dir=results,
    )
    ground_truth = truth(tmp_path, replied, held, broken, skipped)
    chosen = f"{replied.id},{held.id},{broken.id}"

    code = evaluate(agentpilot_engine, ground_truth, results, agent, "--tickets", chosen)

    assert code == 0
    result = report(results)
    summary = result.summary
    assert summary.status == "completed" and summary.finished_at is not None
    assert summary.name == "default" and summary.model_requested == "sonnet"
    assert summary.tickets_requested == [replied.id, held.id, broken.id]
    assert summary.isolation is not None and summary.isolation.passed
    assert summary.chaos_clean_at_end is True
    assert summary.simulation_now == datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    assert len(summary.code.prompt_sha256) == 64
    assert summary.runs_by_status == {"succeeded": 2, "failed": 1}
    known = sum(t.cost_usd or 0 for t in result.tickets)
    assert summary.total_cost_usd == pytest.approx(known + CHECK_COST)
    by_id = {t.ticket_id: t for t in result.tickets}
    assert list(by_id) == [replied.id, held.id, broken.id]

    # Réponse réellement envoyée au client
    answer = by_id[replied.id]
    assert answer.status == "succeeded" and answer.decision is not None
    assert answer.decision.action == "reply"
    assert [(a.tool, a.outcome) for a in answer.actions] == [("reply_to_ticket", "succeeded")]
    assert answer.replies == [REPLY_BODY]
    # Lectures d'outils gardées avec leur réponse : `facts_used` vérifiable à la notation
    assert answer.reads is not None
    [lookup] = answer.reads
    assert (lookup.tool, lookup.outcome, lookup.params) == (
        "get_ticket",
        "succeeded",
        {"ticket_id": replied.id},
    )
    assert replied.id in values(lookup.response, "id")
    assert by_id[held.id].reads == []
    assert answer.ticket_after is not None
    assert answer.trace_id and answer.run_id and answer.cost_usd == pytest.approx(0.0217)

    # Remboursement de plus de 50 € : mis en attente, jamais exécuté, jamais approuvé
    pending = by_id[held.id]
    assert [(a.tool, a.outcome) for a in pending.actions] == [("refund_order", "held")]
    [waiting] = pending.held
    assert waiting.tool == "refund_order" and waiting.reasons == ["over_threshold"]
    assert waiting.status == "pending" and waiting.params["amount_cents"] == 8_000
    assert pending.replies == []
    assert shop.refunds(large) == 0
    with session_factory() as session:
        assert session.scalars(select(PendingAction.status)).all() == ["pending"]

    # Échec de l'agent : enregistré avec son motif, sans décision
    failed = by_id[broken.id]
    assert failed.status == "failed" and failed.decision is None
    assert failed.error_type == "invalid_output" and failed.error

    # Ticket hors sous-ensemble : jamais lancé
    assert skipped.id not in agent.ticket_prompts()
    assert agent.ticket_prompts() == sorted([replied.id, held.id, broken.id])
    assert agent.max_running <= MAX_CONCURRENT_RUNS
    assert agent.reports_read > 0
    with session_factory() as session:
        runs = session.scalars(select(Run)).all()
    assert {r.trigger for r in runs} == {RunTrigger.EVAL}
    assert [r.agent for r in runs].count(AgentKind.SUPPORT_CHECK) == 1
    assert not list(results.glob(".*.tmp"))

    # Écran : progression lisible et résumé, jamais de log JSON
    out = capsys.readouterr().out
    [path] = results.glob("*.json")
    assert "Vérification d'isolement réussie (0,0100 $)" in out
    for ticket in (replied, held, broken):
        assert f"Ticket {ticket.id} lancé (" in out
    assert re.search(rf"Ticket {replied.id} terminé \(\d/3\) : succeeded, 0,0217 \$", out)
    assert f"Ticket {held.id} terminé" in out and "1 action(s) en attente d'un humain" in out
    assert re.search(rf"Ticket {broken.id} terminé .*: failed, .*erreur invalid_output", out)
    assert "État : completed" in out
    assert "Tickets : 2 succeeded, 1 failed" in out
    assert out.rstrip().endswith(f"Résultats : {path}")
    assert '"event"' not in out


def test_agent_gets_the_service_prompt_and_nothing_from_the_ground_truth(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))})

    code = evaluate(agentpilot_engine, truth(tmp_path, ticket), tmp_path / "out", agent)

    assert code == 0
    assert agent.prompts == [CHECK_PROMPT, support_agent.ticket_prompt(ticket.id)]
    for options in agent.options:
        # Mêmes hooks que le service, et rien de secret ni de la vérité terrain
        assert options.hooks
        given = repr(options) + "".join(agent.prompts)
        for secret in (TOKEN, "CHAOS_TOKEN", "tickets.jsonl", SECRET_FACT, SECRET_FORBIDDEN):
            assert secret not in given
        assert "CHAOS_TOKEN" not in options.env


def test_another_prompt_version_is_evaluated_and_recorded(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """`--prompt v1` : la vérification d'isolement et le ticket reçoivent la v1, dont la
    version et l'empreinte vont dans les résultats ; sans option, la v2 du service."""
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))})
    results = tmp_path / "out"

    code = evaluate(agentpilot_engine, truth(tmp_path, ticket), results, agent, "--prompt", "v1")

    assert code == 0
    v1 = support_agent.build_system_prompt(TODAY, "v1")
    assert [o.system_prompt for o in agent.options] == [v1, v1]
    summary = report(results).summary
    assert summary.prompt_version == "v1"
    assert summary.code.prompt_sha256 == run.code_version("v1").prompt_sha256
    assert summary.code.prompt_sha256 != run.code_version().prompt_sha256
    assert run.code_version().prompt_sha256 == run.code_version("v2").prompt_sha256


def test_unknown_prompt_version_is_refused() -> None:
    with pytest.raises(SystemExit):
        run.parser().parse_args(["--prompt", "v9"])
    assert run.parser().parse_args([]).prompt == "v2"


def test_budget_cap_stops_launching(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(3)]
    agent = Agent({t.id: ([], decision(t, **REPLY_DECISION)) for t in tickets})
    results = tmp_path / "results"

    # Vérification (0,01 $) + un ticket réservé à 0,50 $ : 0,51 $. Une fois le premier fini
    # (0,0317 $ dépensés), plus rien en cours et la réserve ne tient plus sous 0,52 $
    code = evaluate(
        agentpilot_engine, truth(tmp_path, *tickets), results, agent, "--max-budget-usd", "0.52"
    )

    assert code == 2
    result = report(results)
    assert result.summary.status == "budget_exhausted"
    statuses = [(t.status.value, t.not_run_reason) for t in result.tickets]
    assert statuses == [("succeeded", None), ("not_run", "budget"), ("not_run", "budget")]
    assert result.summary.total_cost_usd <= 0.52
    assert agent.ticket_prompts() == [tickets[0].id]


def test_ticket_waits_for_the_running_ones_when_the_reserve_does_not_fit(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    """Premier lancement en CI : plafond 1 $, 2 exécutions en parallèle au plus. Le deuxième
    ticket ne tient pas à côté du premier (0,01 $ + 2 × 0,50 $ > 1 $) : il attend la fin du
    premier au lieu d'arrêter le passage, puis chacun part seul."""
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(4)]
    agent = Agent({t.id: ([], decision(t, **REPLY_DECISION)) for t in tickets})
    results = tmp_path / "results"

    code = evaluate(
        agentpilot_engine, truth(tmp_path, *tickets), results, agent, "--max-budget-usd", "1"
    )

    assert code == 0
    result = report(results)
    assert result.summary.status == "completed"
    assert [t.status.value for t in result.tickets] == ["succeeded"] * 4
    assert agent.ticket_prompts() == sorted(t.id for t in tickets)
    # Réserve toujours respectée : jamais deux exécutions ensemble sous ce plafond
    assert agent.max_running == 1
    assert result.summary.total_cost_usd <= 1


def test_ctrl_c_interrupts_the_runs(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(3)]
    agent = Agent({t.id: ([], decision(t, **REPLY_DECISION)) for t in tickets}, interrupt=True)
    results = tmp_path / "results"

    code = evaluate(agentpilot_engine, truth(tmp_path, *tickets), results, agent)

    assert code == 2
    result = report(results)
    assert result.summary.status == "interrupted"
    assert {t.status.value for t in result.tickets} <= {"interrupted", "not_run"}
    assert any(t.status == "interrupted" for t in result.tickets)
    with session_factory() as session:
        statuses = session.scalars(select(Run.status).where(Run.agent == AgentKind.SUPPORT))
        assert RunStatus.RUNNING not in set(statuses)


# --- Passages répétés -----------------------------------------------------------------------


def test_passes_reset_between_them_and_share_one_series(
    ready: None,
    shop: Shop,
    session: Session,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(2)]
    scripts = {
        t.id: (
            [call("reply_to_ticket", ticket_id=t.id, body=REPLY_BODY)],
            decision(t, **REPLY_DECISION),
        )
        for t in tickets
    }
    agent = Agent(scripts)
    reseed = Reseed(session)
    results = tmp_path / "results"

    code = evaluate(
        agentpilot_engine, truth(tmp_path, *tickets), results, agent, "--passes", "2", seed=reseed
    )

    assert code == 0
    # Remise à zéro entre les deux passages seulement
    assert reseed.calls == 1
    files = sorted(results.glob("*.json"))
    assert [f.name[-8:] for f in files] == ["-p1.json", "-p2.json"]
    reports = [EvalReport.model_validate_json(f.read_text(encoding="utf-8")) for f in files]
    assert [r.summary.pass_index for r in reports] == [1, 2]
    assert {r.summary.passes for r in reports} == {2}
    assert reports[0].summary.series == reports[1].summary.series is not None
    assert all(r.summary.status == "completed" for r in reports)
    assert agent.ticket_prompts() == sorted([t.id for t in tickets] * 2)
    # Tables vidées par la remise à zéro : il ne reste que le second passage
    assert count_runs(session_factory) == 3
    out = capsys.readouterr().out
    assert "passage 2/2" in out and "Remise à zéro avant le passage 2" in out
    assert "Noter : python -m evals.grade" in out and str(files[1]) in out


def test_one_budget_for_the_whole_series(
    ready: None,
    shop: Shop,
    session: Session,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))})
    results = tmp_path / "results"

    # Passage 1 : 0,0317 $ ; passage 2 : 0,0317 + 0,50 réservés > 0,53 : rien n'est lancé
    code = evaluate(
        agentpilot_engine,
        truth(tmp_path, ticket),
        results,
        agent,
        "--passes",
        "3",
        "--max-budget-usd",
        "0.53",
        seed=Reseed(session),
    )

    assert code == 2
    first, second = (
        EvalReport.model_validate_json(f.read_text(encoding="utf-8"))
        for f in sorted(results.glob("*.json"))
    )
    assert first.summary.status == "completed"
    assert second.summary.status == "budget_exhausted"
    assert second.summary.isolation is None
    assert second.tickets[0].not_run_reason == "budget"
    assert agent.prompts.count(CHECK_PROMPT) == 1


def test_ctrl_c_stops_the_series(
    ready: None,
    shop: Shop,
    session: Session,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))}, interrupt=True)
    reseed = Reseed(session)
    results = tmp_path / "results"

    code = evaluate(
        agentpilot_engine, truth(tmp_path, ticket), results, agent, "--passes", "2", seed=reseed
    )

    assert code == 2
    assert reseed.calls == 0
    [only] = results.glob("*.json")
    assert only.name.endswith("-p1.json")


def test_checks_are_redone_before_each_pass(
    ready: None,
    shop: Shop,
    session: Session,
    agentpilot_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    reply_call = call("reply_to_ticket", ticket_id=ticket.id, body=REPLY_BODY)
    agent = Agent({ticket.id: ([reply_call], decision(ticket, **REPLY_DECISION))})

    # Seed sans effet : le ticket a déjà sa réponse au passage 2
    code = evaluate(
        agentpilot_engine,
        truth(tmp_path, ticket),
        tmp_path / "results",
        agent,
        "--passes",
        "2",
        seed=Reseed(session, works=False),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "Évaluation refusée (passage 2) : ShopSim n'est pas remis à zéro" in error
    assert agent.ticket_prompts() == [ticket.id]


def test_shopsim_unavailable_aborts(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(4)]
    agent = Agent({t.id: ([], decision(t, **REPLY_DECISION)) for t in tickets})
    calls = iter(range(100))

    def clock(url: str) -> object:
        # La vérification d'isolement lit l'horloge, puis ShopSim tombe
        if next(calls) == 0:
            return TODAY
        raise AgentRunError("Horloge de ShopSim illisible")

    monkeypatch.setattr(support_agent, "simulation_today", clock)
    results = tmp_path / "results"

    code = evaluate(agentpilot_engine, truth(tmp_path, *tickets), results, agent)

    assert code == 2
    result = report(results)
    assert result.summary.status == "aborted"
    assert result.summary.stop_reason == "ShopSim injoignable pendant l'évaluation"
    # Jamais une faute de l'agent : interrompu, hors de la notation
    assert result.tickets[0].error_type == "shopsim_unavailable"
    assert result.tickets[0].status == "interrupted"
    assert result.tickets[-1].status == "not_run"
    assert result.tickets[-1].not_run_reason == "aborted"


def test_api_error_stops_the_series_and_is_never_an_agent_failure(
    ready: None,
    shop: Shop,
    session: Session,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    """Cas réel du 2026-10-29 : limite de dépense atteinte au milieu du passage 1."""
    tickets = [shop.ticket(shop.payment(1_000 + n)) for n in range(4)]
    agent = Agent(
        {t.id: ([], decision(t, **REPLY_DECISION)) for t in tickets}, api_down={tickets[1].id}
    )
    reseed = Reseed(session)
    results = tmp_path / "results"

    code = evaluate(
        agentpilot_engine,
        truth(tmp_path, *tickets),
        results,
        agent,
        "--passes",
        "2",
        seed=reseed,
    )

    assert code == 2
    # Pas de passage 2 : ni remise à zéro, ni second fichier
    assert reseed.calls == 0
    [only] = results.glob("*.json")
    result = EvalReport.model_validate_json(only.read_text(encoding="utf-8"))
    assert result.summary.status == "aborted"
    assert result.summary.stop_reason == (
        "API Anthropic indisponible (limite de dépense, clé refusée ou panne) pendant "
        "l'évaluation : HTTP 400"
    )
    by_id = {t.ticket_id: t for t in result.tickets}
    down = by_id[tickets[1].id]
    assert down.status == "interrupted" and down.error_type == "api_unavailable"
    # Plus aucun lancement après l'erreur (au plus 2 exécutions en même temps)
    assert by_id[tickets[3].id].status == "not_run"
    assert by_id[tickets[3].id].not_run_reason == "aborted"
    assert "failed" not in {t.status for t in result.tickets}


def test_isolation_check_without_the_api_is_not_an_isolation_failure(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))}, check_api_down=True)
    results = tmp_path / "results"

    code = evaluate(agentpilot_engine, truth(tmp_path, ticket), results, agent)

    assert code == 2
    result = report(results)
    assert result.summary.status == "aborted"
    assert result.summary.stop_reason == (
        "API Anthropic indisponible (limite de dépense, clé refusée ou panne) pendant "
        "l'évaluation : HTTP 400 : aucun ticket lancé"
    )
    assert agent.ticket_prompts() == []


def test_failed_isolation_check_launches_no_ticket(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    agent = Agent({ticket.id: ([], decision(ticket, **REPLY_DECISION))}, check_ok=False)
    results = tmp_path / "results"

    code = evaluate(agentpilot_engine, truth(tmp_path, ticket), results, agent)

    assert code == 2
    result = report(results)
    assert result.summary.status == "aborted"
    assert result.summary.isolation is not None and not result.summary.isolation.passed
    assert result.tickets[0].status == "not_run"
    assert agent.ticket_prompts() == []


# --- Refus avant tout lancement -------------------------------------------------------------


def refused(
    capsys: pytest.CaptureFixture[str],
    engine: Engine,
    ground_truth: Path,
    tmp_path: Path,
    *args: str,
    environ: dict[str, str] | None = None,
) -> str:
    agent = Agent({})
    results = tmp_path / "results"
    code = evaluate(engine, ground_truth, results, agent, *args, environ=environ)
    assert code == 1
    assert agent.prompts == []
    assert not results.exists()
    return capsys.readouterr().err


def test_refused_with_chaos_token_in_the_environment(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ground_truth = truth(tmp_path, shop.ticket(shop.payment(1_000)))

    error = refused(
        capsys, agentpilot_engine, ground_truth, tmp_path, environ={"CHAOS_TOKEN": TOKEN}
    )

    assert "CHAOS_TOKEN est dans l'environnement" in error and TOKEN not in error
    assert count_runs(session_factory) == 0


def test_refused_while_the_service_runs(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ground_truth = truth(tmp_path, shop.ticket(shop.payment(1_000)))

    with service_lock(agentpilot_engine) as locked:
        assert locked
        error = refused(capsys, agentpilot_engine, ground_truth, tmp_path)

    assert "service AgentPilot est lancé" in error
    assert count_runs(session_factory) == 0


def test_refused_without_agentpilot_reset(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ticket = shop.ticket(shop.payment(1_000))
    new_run(session_factory, AgentKind.SUPPORT, 999, RunTrigger.LOOP, check_budget=False)

    error = refused(capsys, agentpilot_engine, truth(tmp_path, ticket), tmp_path)

    assert "AgentPilot n'est pas remis à zéro (1 exécutions)" in error
    assert count_runs(session_factory) == 1


@pytest.mark.parametrize("state", ["extra", "answered", "escalated"])
def test_refused_without_shopsim_reset(
    state: str,
    ready: None,
    shop: Shop,
    make_ticket: MakeTicket,
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payment = shop.payment(1_000)
    customer, order = payment.order.customer, payment.order
    at = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
    if state == "extra":
        known = [shop.ticket(payment)]
        # Ticket du générateur de trafic, hors vérité terrain
        shop.ticket(shop.payment(2_000))
    elif state == "answered":
        messages = [(MessageAuthor.CUSTOMER, "Où est mon colis ?", at)]
        messages.append((MessageAuthor.AGENT, "Il arrive.", at))
        known = [make_ticket(customer, order, messages=messages)]
    else:
        known = [make_ticket(customer, order, status=TicketStatus.ESCALATED)]

    error = refused(capsys, agentpilot_engine, truth(tmp_path, *known), tmp_path)

    expected = {
        "extra": "tickets ouverts hors vérité terrain",
        "answered": "ayant déjà une réponse",
        "escalated": "déjà traités",
    }[state]
    assert "ShopSim n'est pas remis à zéro" in error and expected in error
    assert count_runs(session_factory) == 0


def test_refused_when_the_clock_is_not_frozen(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SHOPSIM_FREEZE_CLOCK")
    ground_truth = truth(tmp_path, shop.ticket(shop.payment(1_000)))

    error = refused(capsys, agentpilot_engine, ground_truth, tmp_path)

    assert "Horloge de ShopSim non figée" in error and "SHOPSIM_FREEZE_CLOCK=1" in error


def test_refused_with_an_active_fault(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ground_truth = truth(tmp_path, shop.ticket(shop.payment(1_000)))
    shop.session.add(
        ChaosFault(
            scenario=ChaosScenario.PAYMENT_LATENCY,
            activated_at=datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
            request_count=0,
        )
    )
    shop.session.flush()

    error = refused(capsys, agentpilot_engine, ground_truth, tmp_path)

    assert "Pannes Chaos actives : payment_latency" in error


@pytest.mark.parametrize("dotenv", ["", "CHAOS_TOKEN=un-autre-jeton\n"])
def test_refused_when_faults_cannot_be_checked(
    dotenv: str,
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file.ENV_FILE.write_text(dotenv, encoding="utf-8")
    ground_truth = truth(tmp_path, shop.ticket(shop.payment(1_000)))

    error = refused(capsys, agentpilot_engine, ground_truth, tmp_path)

    assert ("absent de .env" if not dotenv else "Jeton Chaos refusé") in error
    assert TOKEN not in error and "un-autre-jeton" not in error


def test_refused_for_tickets_outside_the_ground_truth(
    ready: None,
    shop: Shop,
    agentpilot_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ticket = shop.ticket(shop.payment(1_000))

    error = refused(
        capsys, agentpilot_engine, truth(tmp_path, ticket), tmp_path, "--tickets", "999999"
    )

    assert "absents de la vérité terrain : 999999" in error


def test_json_logs_go_only_to_the_log_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Logs JSON dans logs/ seulement (`stream=None`) ; les refus restent sur stderr."""
    from observability import logs

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(logs, "configure", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(run, "init_db", lambda bind: None)
    monkeypatch.setattr(run, "service_lock", lambda bind: HeldLock())
    ground_truth = Path(__file__).resolve().parents[1] / "tickets.jsonl"

    code = run.main(["--yes", "--tickets", "1"], environ={}, ground_truth=ground_truth)

    assert code == 1
    assert calls == [{"stream": None}]
    assert "Évaluation refusée : le service AgentPilot est lancé" in capsys.readouterr().err


class HeldLock:
    """Verrou du service déjà tenu (`service_lock` renvoie False)."""

    def __enter__(self) -> bool:
        return False

    def __exit__(self, *exc: object) -> None:
        return None


# --- Entrées et fichier ---------------------------------------------------------------------


def test_ticket_selection() -> None:
    known = [1, 2, 3, 4, 5, 19, 20]

    assert run.parse_tickets(None, known) == known
    assert run.parse_tickets("1-3,19", known) == [1, 2, 3, 19]
    assert run.parse_tickets(" 5 , 4 ", known) == [4, 5]
    for wrong in ("3-1", "a", "1,,2", "6"):
        with pytest.raises(run.Refused):
            run.parse_tickets(wrong, known)


def test_ground_truth_gives_only_ticket_ids(tmp_path: Path) -> None:
    path = tmp_path / "tickets.jsonl"
    path.write_text('{"ticket_id": 7, "category": "x"}\n\n{"ticket_id": 3}\n', encoding="utf-8")

    assert run.ground_truth_ids(path) == [3, 7]


def test_results_file_is_replaced_atomically(tmp_path: Path) -> None:
    path = tmp_path / "results" / "r.json"
    summary = {
        "name": "essai",
        "model_requested": "sonnet",
        "code": {"commit": None, "dirty": None, "prompt_sha256": "0" * 64},
        "simulation_now": "2026-07-06T12:00:00Z",
        "started_at": "2026-10-28T12:00:00Z",
        "status": "running",
        "tickets_requested": [1],
        "max_budget_usd": 1.0,
    }
    first = EvalReport.model_validate({"summary": summary})

    run.write_atomic(path, first)
    run.write_atomic(path, first.model_copy(update={"tickets": []}))

    assert EvalReport.model_validate_json(path.read_text(encoding="utf-8")) == first
    assert [p.name for p in path.parent.iterdir()] == ["r.json"]
