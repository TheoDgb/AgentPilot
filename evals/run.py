"""Lanceur d'évaluation (J5.1) : l'agent Support sur les tickets de la vérité terrain.

    python -m evals.run                                   # les 48 tickets, plafond 10 $
    python -m evals.run --tickets 1-5,19 --max-budget-usd 2 --name essai
    python -m evals.run --model opus --name opus --yes    # sans confirmation (scripts, CI)

Même code que le service : `Worker` d'AgentPilot (`agentpilot/worker.py`), donc mêmes hooks,
mêmes outils, même isolement, même journal, au plus 2 exécutions en même temps. Les actions
sensibles sont mises en attente comme d'habitude ; personne ne les approuve. Une vérification
d'isolement (`sdk_runner.check_context`) précède le premier ticket.

Refus avant tout lancement si :
1. `CHAOS_TOKEN` est dans l'environnement (la CLI de l'agent hérite de `os.environ`) ;
2. le service AgentPilot est lancé (verrou de la base, tenu ensuite jusqu'à la fin) ;
3. la remise à zéro complète n'est pas faite (`python -m agentpilot.reset`) : tables
   d'AgentPilot vides, tickets ouverts de ShopSim exactement ceux de la vérité terrain, sans
   réponse (les tickets historiques du seed, déjà traités, sont admis) ;
4. l'horloge de ShopSim n'est pas figée (`SHOPSIM_FREEZE_CLOCK=1`) ;
5. une panne Chaos est active, ou ne peut pas être vérifiée (jeton lu dans la seule ligne
   `CHAOS_TOKEN=` de `.env`, gardé dans ce processus, jamais dans son environnement).

La vérité terrain ne sert ici qu'à connaître les numéros des tickets : l'agent reçoit la même
consigne que dans le service, sans outil de fichier. Résultats dans
`evals/results/<date>-<nom>.json` (non versionné), réécrit après chaque ticket : décision,
ce qui s'est réellement passé, coût, durée, erreurs, trace. Schéma : `evals/results.py`.
Pas de notation ici (J5.2).

Écran : progression (ticket lancé, ticket terminé avec son statut et son coût), puis un
résumé avec le chemin du fichier de résultats ; refus sur stderr. Les logs JSON vont seulement
dans `logs/agentpilot.jsonl` (`python -m observability.query --trace <trace_id>`).

Budget : un ticket n'est lancé que si la dépense, plus le plafond d'une exécution pour chacune
en cours et pour celle-ci, reste sous `--max-budget-usd`. Le budget du jour du service ne
s'applique pas, mais ces exécutions comptent dans sa dépense (elles sont en base).
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import env_file, sdk_runner
from agentpilot.approval import REPLY, short_name
from agentpilot.control import RUN_RESERVE_USD
from agentpilot.db import engine, init_db, make_session_factory, service_lock
from agentpilot.journal import DbJournal
from agentpilot.models import (
    CONTROL_ID,
    AgentKind,
    CallOutcome,
    Control,
    PendingAction,
    Run,
    RunStatus,
    RunTrigger,
    ToolCall,
)
from agentpilot.schemas import SupportDecision
from agentpilot.sdk_runner import AgentResult, ErrorType, QueryFn
from agentpilot.support_agent import ACTION_TOOL_NAMES, AGENT_DIR, REPO_ROOT, SKILL_PATH
from agentpilot.worker import MAX_CONCURRENT_RUNS, Worker, finish, new_run
from evals.results import (
    ActionCall,
    CodeVersion,
    EvalReport,
    EvalStatus,
    EvalSummary,
    HeldAction,
    IsolationCheck,
    NotRunReason,
    TicketResult,
    TicketStatus,
)
from mcp_server.shopsim_client import ShopSimError, get_json, shopsim_url
from observability import trace
from shopsim.app.clock import SIMULATION_NOW
from shopsim.chaos_cli import TOKEN_ENV, read_token

GROUND_TRUTH = REPO_ROOT / "evals" / "tickets.jsonl"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_BUDGET_USD = Decimal("10")
DEFAULT_NAME = "default"
NAME = re.compile(r"[A-Za-z0-9_.-]{1,50}")
TICKETS_PAGE = 100
CHAOS_TIMEOUT_S = 10
YES = frozenset({"o", "oui", "y", "yes"})

RUN_STATUS = {
    RunStatus.SUCCEEDED: TicketStatus.SUCCEEDED,
    RunStatus.FAILED: TicketStatus.FAILED,
    RunStatus.INTERRUPTED: TicketStatus.INTERRUPTED,
    # Jamais à la fin d'une exécution : par prudence, comptée comme interrompue
    RunStatus.RUNNING: TicketStatus.INTERRUPTED,
}


class Refused(Exception):
    """Évaluation refusée avant tout lancement ; message affiché tel quel."""


# --- Entrées -------------------------------------------------------------------------------


def ground_truth_ids(path: Path) -> list[int]:
    """Numéros des tickets de la vérité terrain, rien d'autre (le reste sert à la notation)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted(int(json.loads(line)["ticket_id"]) for line in lines if line.strip())


def parse_tickets(spec: str | None, known: Sequence[int]) -> list[int]:
    """`1-5,19` → [1, 2, 3, 4, 5, 19] ; tous les tickets connus sans `spec`."""
    if spec is None:
        return sorted(known)
    chosen: set[int] = set()
    for part in (p.strip() for p in spec.split(",")):
        found = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if found is None:
            raise Refused(f"--tickets : « {part} » n'est ni un numéro ni une plage (ex. 1-5,19)")
        start, end = int(found[1]), int(found[2] or found[1])
        if end < start:
            raise Refused(f"--tickets : plage « {part} » à l'envers")
        chosen.update(range(start, end + 1))
    unknown = sorted(chosen - set(known))
    if unknown:
        raise Refused(f"Tickets absents de la vérité terrain : {', '.join(map(str, unknown))}")
    return sorted(chosen)


def budget(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"montant invalide : {value}") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("le plafond doit être positif")
    return amount


def config_name(value: str) -> str:
    if NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("lettres, chiffres, « _ », « . » ou « - » (50 au plus)")
    return value


# --- Vérifications préalables --------------------------------------------------------------


def check_environment(environ: Mapping[str, str]) -> None:
    if TOKEN_ENV in environ:
        raise Refused(
            f"{TOKEN_ENV} est dans l'environnement : l'agent en hériterait. Lancer sans "
            f"{TOKEN_ENV} dans le shell (l'évaluation le lit elle-même dans .env)."
        )


def check_agentpilot_reset(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        counts = {
            label: session.scalar(select(func.count()).select_from(model)) or 0
            for label, model in (
                ("exécutions", Run),
                ("appels d'outils", ToolCall),
                ("actions en attente", PendingAction),
            )
        }
        stopped = session.scalar(select(Control.emergency_stop).where(Control.id == CONTROL_ID))
    left = [f"{count} {label}" for label, count in counts.items() if count]
    if left:
        raise Refused(
            f"AgentPilot n'est pas remis à zéro ({', '.join(left)}) : lancer "
            "python -m agentpilot.reset"
        )
    if stopped:
        raise Refused("Arrêt d'urgence actif : lancer python -m agentpilot.reset")


async def check_shopsim_reset(expected: Sequence[int]) -> None:
    """Tickets ouverts de ShopSim : exactement ceux de la vérité terrain, sans réponse. Les
    tickets historiques du seed (déjà traités, hors vérité terrain) sont admis."""
    tickets: list[dict[str, object]] = []
    offset = 0
    while True:
        page = await get_json(
            "/tickets", "Liste des tickets introuvable.", {"limit": TICKETS_PAGE, "offset": offset}
        )
        tickets += page["items"]
        offset += TICKETS_PAGE
        if offset >= page["total"] or not page["items"]:
            break
    found = {int(str(ticket["id"])) for ticket in tickets}
    opened = {int(str(ticket["id"])) for ticket in tickets if ticket["status"] == "open"}
    problems = []
    if extra := sorted(opened - set(expected)):
        problems.append(f"tickets ouverts hors vérité terrain {extra} (générateur de trafic ?)")
    if missing := sorted(set(expected) - found):
        problems.append(f"tickets manquants {missing}")
    if not_open := sorted((set(expected) & found) - opened):
        problems.append(f"tickets déjà traités {not_open}")
    answered = []
    for ticket_id in sorted(opened & set(expected)):
        ticket = await get_json(f"/tickets/{ticket_id}", "Ticket introuvable.")
        if any(message["author"] != "customer" for message in ticket["messages"]):
            answered.append(ticket_id)
    if answered:
        problems.append(f"tickets ayant déjà une réponse {answered}")
    if problems:
        raise Refused(
            f"ShopSim n'est pas remis à zéro : {' ; '.join(problems)}. Lancer "
            "python -m agentpilot.reset"
        )


async def check_clock() -> datetime:
    clock = await get_json("/clock", "Horloge de ShopSim introuvable.")
    now = datetime.fromisoformat(str(clock["now"]).replace("Z", "+00:00"))
    if not clock["frozen"] or now != SIMULATION_NOW:
        raise Refused(
            f"Horloge de ShopSim non figée ({clock['now']}) : relancer ShopSim avec "
            f"SHOPSIM_FREEZE_CLOCK=1, après python -m agentpilot.reset (vérité terrain au "
            f"{SIMULATION_NOW:%Y-%m-%d %H:%M} UTC)"
        )
    return now


async def active_faults(token: str | None) -> list[str]:
    """Pannes Chaos actives ; le jeton n'est envoyé qu'à ShopSim, jamais journalisé."""
    if not token:
        raise Refused(
            f"{TOKEN_ENV} absent de .env : impossible de vérifier qu'aucune panne n'est active"
        )
    headers = {"X-Chaos-Token": token} | trace.headers()
    try:
        async with httpx.AsyncClient(timeout=CHAOS_TIMEOUT_S) as client:
            response = await client.get(f"{shopsim_url()}/chaos", headers=headers)
    except httpx.HTTPError as exc:
        where = f"{shopsim_url()}/chaos"
        raise Refused(f"ShopSim injoignable ({where}) : {type(exc).__name__}") from None
    if response.status_code == 403:
        raise Refused(
            f"Jeton Chaos refusé par ShopSim : {TOKEN_ENV} de .env doit être celui de ShopSim"
        )
    if response.status_code != 200:
        raise Refused(f"État des pannes illisible ({response.status_code} sur /chaos)")
    return sorted(str(fault["scenario"]) for fault in response.json()["active"])


# --- Résultats ------------------------------------------------------------------------------


def code_version() -> CodeVersion:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    # Fichiers non suivis ignorés : seules comptent les modifications du code versionné
    status = git("status", "--porcelain", "--untracked-files=no")
    digest = hashlib.sha256()
    for path in (AGENT_DIR / "CLAUDE.md", SKILL_PATH):
        digest.update(path.read_bytes())
    return CodeVersion(
        commit=commit.strip() if commit else None,
        dirty=None if status is None else bool(status.strip()),
        prompt_sha256=digest.hexdigest(),
    )


def charged_cost(run: Run) -> Decimal:
    """Même règle que le budget du jour (`control.spent_today`) : le coût connu, sinon le
    plafond d'une exécution si l'agent a pu consommer."""
    if run.total_cost_usd is not None:
        return run.total_cost_usd
    if run.status in (RunStatus.RUNNING, RunStatus.INTERRUPTED) or (
        run.status is RunStatus.FAILED and run.model is not None
    ):
        return RUN_RESERVE_USD
    return Decimal(0)


def write_atomic(path: Path, report: EvalReport) -> None:
    """Fichier toujours lisible : écrit à côté, puis renommé."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ticket_record(run: Run, result: AgentResult | None) -> TicketResult:
    """Ce que l'agent a décidé et ce qui s'est réellement passé, d'après le journal."""
    calls = [call for call in run.tool_calls if call.tool in ACTION_TOOL_NAMES]
    status = RUN_STATUS[run.status]
    return TicketResult(
        ticket_id=run.ticket_id or 0,
        status=status,
        run_id=run.id,
        trace_id=run.trace_id,
        decision=(
            SupportDecision.model_validate(run.output)
            if status is TicketStatus.SUCCEEDED and run.output
            else None
        ),
        error=run.error,
        error_type=result.error_type if result is not None else None,
        actions=[
            ActionCall(
                tool=short_name(call.tool),
                params=call.input,
                outcome=call.outcome.value,
                error=call.error,
            )
            for call in calls
        ],
        held=[
            HeldAction(
                tool=action.tool,
                params=action.params,
                reasons=action.reasons,
                facts=action.facts,
                status=action.status.value,
            )
            for action in run.pending_actions
        ],
        replies=[
            str(call.input.get("body"))
            for call in calls
            if short_name(call.tool) == REPLY and call.outcome is CallOutcome.SUCCEEDED
        ],
        tool_calls=len(run.tool_calls),
        cost_usd=None if run.total_cost_usd is None else float(run.total_cost_usd),
        duration_ms=run.duration_ms,
        num_turns=run.num_turns,
        model=run.model,
        propose_only=run.propose_only,
    )


# --- Évaluation -----------------------------------------------------------------------------


def say(line: str, *, error: bool = False) -> None:
    """Une ligne lisible à l'écran (progression, résumé) ; jamais de log JSON."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)


def usd(amount: float | Decimal | None) -> str:
    return "coût inconnu" if amount is None else f"{float(amount):.4f} $".replace(".", ",")


def ticket_line(record: TicketResult) -> str:
    """Statut et coût d'un ticket terminé, puis ce qui compte pour le relire."""
    parts = [record.status.value, usd(record.cost_usd)]
    if record.duration_ms is not None:
        parts.append(f"{record.duration_ms / 1000:.1f} s".replace(".", ","))
    if record.ticket_after is not None:
        parts.append(f"ticket {record.ticket_after}")
    if record.held:
        parts.append(f"{len(record.held)} action(s) en attente d'un humain")
    if record.error_type is not None:
        parts.append(f"erreur {record.error_type}")
    return ", ".join(parts)


def summary_lines(report: EvalReport, path: Path) -> list[str]:
    """Résumé final affiché à l'écran ; le détail est dans le fichier de résultats."""
    summary = report.summary
    counts: dict[str, int] = {}
    for record in report.tickets:
        key = record.status.value
        if record.not_run_reason is not None:
            key = f"{key} ({record.not_run_reason.value})"
        counts[key] = counts.get(key, 0) + 1
    state = summary.status.value
    if summary.stop_reason:
        state = f"{state} : {summary.stop_reason}"
    lines = [
        f"Évaluation « {summary.name} » terminée",
        f"État : {state}",
        f"Tickets : {', '.join(f'{n} {key}' for key, n in counts.items()) or 'aucun'}",
        f"Coût total : {usd(summary.total_cost_usd)} (plafond {usd(summary.max_budget_usd)})",
    ]
    if summary.finished_at is not None:
        seconds = (summary.finished_at - summary.started_at).total_seconds()
        lines.append(f"Durée : {seconds:.0f} s")
    if summary.chaos_clean_at_end is False:
        lines.append("Attention : une panne Chaos était active à la fin")
    lines.append(f"Résultats : {path}")
    return lines


class Evaluation:
    """Lancement des tickets, budget, arrêts, et fichier de résultats tenu à jour."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        query: QueryFn,
        model: str,
        max_budget: Decimal,
        report: EvalReport,
        path: Path,
    ) -> None:
        self.factory = factory
        self.query = query
        self.model = model
        self.max_budget = max_budget
        self.report = report
        self.path = path
        # Le Worker du service, sur un état minimal : mêmes lancements, mêmes logs
        self.worker = Worker(SimpleNamespace(session_factory=factory, query=query))
        self.worker.model = model
        self.spent = Decimal(0)
        self.results: dict[int, TicketResult] = {}
        # Arrêt avant la fin : plus aucun ticket n'est lancé
        self.halted: EvalStatus | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        # Progression affichée : tickets lancés, tickets terminés
        self.launched = 0
        self.done = 0

    def stop(self) -> None:
        """Ctrl+C : plus aucun lancement, exécutions en cours annulées (`interrupted`)."""
        if self.halted is None:
            self.halted = EvalStatus.INTERRUPTED
            self.report.summary.stop_reason = "Interrompue (Ctrl+C)"
            say("Interruption demandée : exécutions en cours annulées, plus aucun lancement.")
        task = asyncio.get_running_loop().create_task(self.worker.shutdown())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _abort(self, reason: str) -> None:
        if self.halted is None:
            self.halted = EvalStatus.ABORTED
            self.report.summary.stop_reason = reason

    def save(self) -> None:
        summary = self.report.summary
        order = summary.tickets_requested
        self.report.tickets = [self.results[t] for t in order if t in self.results]
        summary.total_cost_usd = float(self.spent)
        counts: dict[str, int] = {}
        for record in self.report.tickets:
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        summary.runs_by_status = counts
        summary.models_used = sorted({r.model for r in self.report.tickets if r.model})
        write_atomic(self.path, self.report)

    async def isolation(self) -> bool:
        """Vérification d'isolement avant le premier ticket ; False : évaluation abandonnée."""
        run = await asyncio.to_thread(
            new_run,
            self.factory,
            AgentKind.SUPPORT_CHECK,
            None,
            RunTrigger.EVAL,
            check_budget=False,
        )
        journal = DbJournal(self.factory, run.id)
        await self.worker.execute(
            run,
            lambda: sdk_runner.check_context(journal=journal, query=self.query, model=self.model),
        )
        with self.factory() as session:
            checked = session.get_one(Run, run.id)
            self.spent += charged_cost(checked)
            passed = checked.status is RunStatus.SUCCEEDED
            cost, error = usd(checked.total_cost_usd), checked.error
            self.report.summary.isolation = IsolationCheck(
                passed=passed, run_id=checked.id, trace_id=checked.trace_id, error=checked.error
            )
        if not passed and self.halted is None:
            self._abort("Vérification d'isolement en échec : aucun ticket lancé")
        self.save()
        if passed:
            say(f"Vérification d'isolement réussie ({cost})")
        else:
            say(f"Vérification d'isolement en échec ({cost}) : {error}", error=True)
        return self.halted is None

    async def ticket(self, ticket_id: int) -> None:
        run = await asyncio.to_thread(
            new_run, self.factory, AgentKind.SUPPORT, ticket_id, RunTrigger.EVAL, check_budget=False
        )
        kept: list[AgentResult] = []
        if self.halted is not None:
            # Arrêt demandé pendant la création : jamais lancée
            await asyncio.to_thread(finish, self.factory, run.id, None)
        else:
            launch = self.worker.ticket_launch(run.id, ticket_id, [])
            self.launched += 1
            say(f"Ticket {ticket_id} lancé ({self.launched}/{self.total()})")

            async def keep() -> AgentResult:
                result = await launch()
                kept.append(result)
                return result

            await self.worker.execute(run, keep)
        result = kept[0] if kept else None
        with self.factory() as session:
            finished = session.get_one(Run, run.id)
            self.spent += charged_cost(finished)
            record = ticket_record(finished, result)
        try:
            record.ticket_after = (await get_json(f"/tickets/{ticket_id}", "Ticket introuvable."))[
                "status"
            ]
        except ShopSimError:
            record.ticket_after = None
        self.results[ticket_id] = record
        if result is not None and result.error_type is ErrorType.SHOPSIM_UNAVAILABLE:
            self._abort("ShopSim injoignable pendant l'évaluation")
        self.save()
        self.done += 1
        say(f"Ticket {ticket_id} terminé ({self.done}/{self.total()}) : {ticket_line(record)}")

    async def run(self, ticket_ids: Sequence[int]) -> EvalStatus:
        budget_exhausted = False
        if await self.isolation():
            for ticket_id in ticket_ids:
                while len(self.running()) >= MAX_CONCURRENT_RUNS and self.halted is None:
                    await asyncio.wait(self.running(), return_when=asyncio.FIRST_COMPLETED)
                if self.halted is not None:
                    break
                # Au pire, chaque exécution en cours et celle-ci coûtent leur plafond
                reserved = RUN_RESERVE_USD * (len(self.running()) + 1)
                if self.spent + reserved > self.max_budget:
                    budget_exhausted = True
                    break
                task = asyncio.create_task(self.ticket(ticket_id))
                task.set_name(f"ticket-{ticket_id}")
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        while self.tasks:
            await asyncio.gather(*list(self.tasks))
        if self.halted is not None:
            status = self.halted
            reason = (
                NotRunReason.INTERRUPTED
                if status is EvalStatus.INTERRUPTED
                else NotRunReason.ABORTED
            )
        elif budget_exhausted:
            status, reason = EvalStatus.BUDGET_EXHAUSTED, NotRunReason.BUDGET
        else:
            status, reason = EvalStatus.COMPLETED, NotRunReason.ABORTED
        for ticket_id in ticket_ids:
            if ticket_id not in self.results:
                self.results[ticket_id] = TicketResult(
                    ticket_id=ticket_id, status=TicketStatus.NOT_RUN, not_run_reason=reason
                )
        self.report.summary.status = status
        self.save()
        return status

    def total(self) -> int:
        return len(self.report.summary.tickets_requested)

    def running(self) -> set[asyncio.Task[None]]:
        return {task for task in self.tasks if task.get_name().startswith("ticket-")}


async def evaluate(
    args: argparse.Namespace,
    ticket_ids: list[int],
    all_ids: list[int],
    factory: sessionmaker[Session],
    query: QueryFn,
    results_dir: Path,
) -> tuple[EvalStatus, Path]:
    """Vérifie l'état de départ, puis lance l'évaluation ; lève `Refused` avant tout lancement."""
    token = read_token({}, env_file.ENV_FILE)
    try:
        await asyncio.to_thread(check_agentpilot_reset, factory)
        await check_shopsim_reset(all_ids)
        simulation_now = await check_clock()
    except ShopSimError as exc:
        raise Refused(f"ShopSim injoignable : {exc}") from None
    if faults := await active_faults(token):
        raise Refused(f"Pannes Chaos actives : {', '.join(faults)} (python -m shopsim.chaos_cli)")

    started = datetime.now(UTC)
    path = results_dir / f"{started:%Y-%m-%dT%H%M%SZ}-{args.name}.json"
    report = EvalReport(
        summary=EvalSummary(
            name=args.name,
            model_requested=args.model,
            code=code_version(),
            simulation_now=simulation_now,
            started_at=started,
            status=EvalStatus.RUNNING,
            tickets_requested=ticket_ids,
            max_budget_usd=float(args.max_budget_usd),
        )
    )
    evaluation = Evaluation(factory, query, args.model, args.max_budget_usd, report, path)
    evaluation.save()
    say(
        f"Évaluation « {args.name} » : {len(ticket_ids)} ticket(s), modèle {args.model}, "
        f"plafond {usd(args.max_budget_usd)}"
    )
    say(f"Résultats : {path}")
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, evaluation.stop)
    try:
        status = await evaluation.run(ticket_ids)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
    try:
        report.summary.chaos_clean_at_end = not await active_faults(token)
    except Refused:
        report.summary.chaos_clean_at_end = None
    report.summary.finished_at = datetime.now(UTC)
    evaluation.save()
    say("")
    for line in summary_lines(report, path):
        say(line)
    return status, path


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Lance l'agent Support sur les tickets de la vérité terrain (sans notation).",
    )
    parser.add_argument("--tickets", help="sous-ensemble, ex. 1-5,19 (tous par défaut)")
    parser.add_argument(
        "--max-budget-usd",
        type=budget,
        default=DEFAULT_BUDGET_USD,
        help=f"plafond de toute l'évaluation ({DEFAULT_BUDGET_USD} $ par défaut)",
    )
    parser.add_argument(
        "--name",
        type=config_name,
        default=DEFAULT_NAME,
        help="nom de la configuration testée (dans le nom du fichier de résultats)",
    )
    parser.add_argument(
        "--model",
        default=sdk_runner.DEFAULT_MODEL,
        help=f"modèle de l'agent ({sdk_runner.DEFAULT_MODEL} par défaut)",
    )
    parser.add_argument("--yes", action="store_true", help="ne demande pas de confirmation")
    return parser


def confirmed(args: argparse.Namespace, count: int) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Sans terminal, ajouter --yes pour confirmer l'évaluation.", file=sys.stderr)
        return False
    question = (
        f"Évaluer {count} ticket(s) avec {args.model}, plafond {args.max_budget_usd} $ ? [o/N] "
    )
    return input(question).strip().lower() in YES


def main(
    argv: Sequence[str] | None = None,
    *,
    bind: Engine = engine,
    query: QueryFn = sdk_runner.sdk_query,
    environ: Mapping[str, str] = os.environ,
    ground_truth: Path = GROUND_TRUTH,
    results_dir: Path = RESULTS_DIR,
    configure_logs: bool = True,
) -> int:
    """0 : évaluation complète ; 1 : refusée ; 2 : arrêtée avant la fin (budget, Ctrl+C,
    abandon)."""
    args = parser().parse_args(argv)
    try:
        all_ids = ground_truth_ids(ground_truth)
        ticket_ids = parse_tickets(args.tickets, all_ids)
        check_environment(environ)
    except Refused as exc:
        print(f"Évaluation refusée : {exc}", file=sys.stderr)
        return 1
    if not confirmed(args, len(ticket_ids)):
        print("Évaluation abandonnée.", file=sys.stderr)
        return 1
    if configure_logs:
        from agentpilot.app import log_dir
        from observability.logs import configure

        # Logs JSON dans logs/ seulement : l'écran est réservé à la progression et aux refus
        configure("agentpilot", log_dir(), stream=None)
    init_db(bind)
    with service_lock(bind) as locked:
        if not locked:
            print(
                "Évaluation refusée : le service AgentPilot est lancé (ou une remise à zéro) : "
                "l'arrêter avant d'évaluer.",
                file=sys.stderr,
            )
            return 1
        factory = make_session_factory(bind)
        try:
            status, path = asyncio.run(
                evaluate(args, ticket_ids, all_ids, factory, query, results_dir)
            )
        except Refused as exc:
            print(f"Évaluation refusée : {exc}", file=sys.stderr)
            return 1
    return 0 if status is EvalStatus.COMPLETED else 2


if __name__ == "__main__":
    sys.exit(main())
