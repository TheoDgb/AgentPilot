"""Métriques Prometheus d'AgentPilot (J4.2), exposées par `GET /metrics`.

Deux sortes de métriques :
- Événements (`EVENTS`) : compteurs du processus, remis à zéro au redémarrage du service
  (`rate()` et `increase()` le gèrent). Exécutions terminées, leur durée et leur coût, appels
  d'outils par résultat, décisions humaines et exécution des actions approuvées.
- État (`state_registry`) : relu en base à chaque collecte, donc juste même après un
  redémarrage. Actions en attente, boucle, arrêt d'urgence, budget du jour, file.

Aucun identifiant (ticket, client, exécution, trace) ni montant en étiquette : toutes les
valeurs viennent d'énumérations fermées, et un nom d'outil inconnu devient `other`.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentpilot.api_schemas import ControlStatus
from agentpilot.approval import REFUND, RESEND, short_name
from agentpilot.models import (
    AgentKind,
    CallOutcome,
    PauseReason,
    PendingAction,
    PendingStatus,
    RunStatus,
    RunTrigger,
)
from agentpilot.support_agent import SUPPORT_TOOL_NAMES

if TYPE_CHECKING:
    # Import circulaire sinon : sdk_runner → hooks → metrics
    from agentpilot.sdk_runner import AgentResult

OTHER_TOOL = "other"
TOOLS = frozenset(short_name(name) for name in SUPPORT_TOOL_NAMES)
# Outils dont l'action peut être mise en attente d'un humain
HELD_TOOLS = (REFUND, RESEND)
# Actions décidées par un humain, encore à exécuter (approuvées : à relancer)
WAITING = (PendingStatus.PENDING, PendingStatus.APPROVED)
# Exécution d'une action approuvée : faite, devenue impossible, ou ShopSim injoignable
EXECUTION_RESULTS = ("executed", "impossible", "unavailable")
DECISIONS = (PendingStatus.APPROVED, PendingStatus.REJECTED)
LOOP_STATES = ("running", "paused_manual", "paused_budget", "waiting")
# De 5 s à 30 min : une exécution de l'agent dure de quelques secondes à quelques minutes
RUN_DURATION_BUCKETS = (5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)

EVENTS = CollectorRegistry()
RUNS_FINISHED = Counter(
    "agentpilot_runs_finished_total",
    "Exécutions terminées, par agent, statut et déclencheur.",
    ["agent", "status", "trigger"],
    registry=EVENTS,
)
RUN_DURATION = Histogram(
    "agentpilot_run_duration_seconds",
    "Durée des exécutions (attente d'une place comprise), en secondes.",
    ["agent", "status"],
    buckets=RUN_DURATION_BUCKETS,
    registry=EVENTS,
)
RUN_COST = Counter(
    "agentpilot_run_cost_usd_total",
    "Coût des exécutions terminées dont le coût est connu, en dollars.",
    ["agent"],
    registry=EVENTS,
)
RUNS_COST_UNKNOWN = Counter(
    "agentpilot_runs_cost_unknown_total",
    "Exécutions terminées sans coût connu (interrompues, ou en échec avant le résultat).",
    ["agent"],
    registry=EVENTS,
)
TOOL_CALLS = Counter(
    "agentpilot_tool_calls_total",
    "Appels d'outils de l'agent inscrits au journal, par outil et résultat (succeeded : "
    "exécuté, held : mis en attente d'un humain, denied : refusé, failed : en échec).",
    ["tool", "outcome"],
    registry=EVENTS,
)
ACTION_DECISIONS = Counter(
    "agentpilot_action_decisions_total",
    "Décisions humaines sur les actions en attente, par outil.",
    ["tool", "decision"],
    registry=EVENTS,
)
ACTION_EXECUTIONS = Counter(
    "agentpilot_action_executions_total",
    "Exécutions par la plateforme des actions approuvées (relances comprises), par résultat.",
    ["tool", "result"],
    registry=EVENTS,
)

# Séries présentes à zéro dès le démarrage : les graphiques et `rate()` n'attendent pas le
# premier événement
for _agent in AgentKind:
    RUN_COST.labels(agent=_agent)
    RUNS_COST_UNKNOWN.labels(agent=_agent)
    for _status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED):
        for _trigger in RunTrigger:
            RUNS_FINISHED.labels(agent=_agent, status=_status, trigger=_trigger)
for _outcome in CallOutcome:
    TOOL_CALLS.labels(tool=OTHER_TOOL, outcome=_outcome)
for _tool in HELD_TOOLS:
    for _decision in DECISIONS:
        ACTION_DECISIONS.labels(tool=_tool, decision=_decision)
    for _result in EXECUTION_RESULTS:
        ACTION_EXECUTIONS.labels(tool=_tool, result=_result)


def tool_label(name: str) -> str:
    """Nom court d'un outil shopsim (`refund_order`), `other` pour tout autre nom."""
    short = short_name(name)
    return short if short in TOOLS else OTHER_TOOL


def run_finished(
    agent: AgentKind, trigger: RunTrigger, result: "AgentResult | None", duration_s: float
) -> None:
    """Exécution terminée ; `None` : annulée (arrêt du service)."""
    if result is None:
        status = RunStatus.INTERRUPTED
    else:
        status = RunStatus.FAILED if result.error else RunStatus.SUCCEEDED
    RUNS_FINISHED.labels(agent=agent, status=status, trigger=trigger).inc()
    RUN_DURATION.labels(agent=agent, status=status).observe(duration_s)
    if result is None or result.total_cost_usd is None:
        RUNS_COST_UNKNOWN.labels(agent=agent).inc()
    else:
        RUN_COST.labels(agent=agent).inc(result.total_cost_usd)


def tool_called(tool: str, outcome: CallOutcome) -> None:
    TOOL_CALLS.labels(tool=tool_label(tool), outcome=outcome).inc()


def action_decided(tool: str, decision: PendingStatus) -> None:
    ACTION_DECISIONS.labels(tool=tool_label(tool), decision=decision).inc()


def action_executed(tool: str, result: str) -> None:
    if result in EXECUTION_RESULTS:
        ACTION_EXECUTIONS.labels(tool=tool_label(tool), result=result).inc()


# --- État, relu à chaque collecte ------------------------------------------------------


def pending_counts(session: Session) -> dict[tuple[str, PendingStatus], int]:
    """Actions en attente d'un humain (pending) ou approuvées à relancer, par outil."""
    rows = session.execute(
        select(PendingAction.tool, PendingAction.status, func.count())
        .where(PendingAction.status.in_(WAITING))
        .group_by(PendingAction.tool, PendingAction.status)
    )
    counts: dict[tuple[str, PendingStatus], int] = {}
    for tool, status, count in rows:
        key = (tool_label(tool), status)
        counts[key] = counts.get(key, 0) + count
    return counts


def oldest_pending(session: Session) -> datetime | None:
    """Date de la plus ancienne action en attente d'un humain."""
    return session.scalar(
        select(func.min(PendingAction.created_at)).where(
            PendingAction.status == PendingStatus.PENDING
        )
    )


def loop_state(status: ControlStatus, now: datetime) -> str:
    if status.loop_paused:
        return "paused_budget" if status.pause_reason is PauseReason.BUDGET else "paused_manual"
    if status.waiting_until is not None and status.waiting_until > now:
        return "waiting"
    return "running"


def state_registry(
    status: ControlStatus | None,
    pending: Mapping[tuple[str, PendingStatus], int] | None,
    now: datetime,
    oldest: datetime | None = None,
) -> CollectorRegistry:
    """Jauges d'état ; `None` : base injoignable, seule `agentpilot_state_up` vaut alors 0."""
    registry = CollectorRegistry()
    up = Gauge(
        "agentpilot_state_up",
        "1 si l'état de la plateforme a pu être lu en base pour cette collecte.",
        registry=registry,
    )
    if status is None or pending is None:
        up.set(0)
        return registry
    up.set(1)

    waiting = Gauge(
        "agentpilot_pending_actions",
        "Actions en attente d'un humain (pending) ou approuvées restées à relancer (approved).",
        ["tool", "status"],
        registry=registry,
    )
    for tool in HELD_TOOLS:
        for pending_status in WAITING:
            waiting.labels(tool=tool, status=pending_status).set(0)
    for (tool, pending_status), count in pending.items():
        waiting.labels(tool=tool, status=pending_status).set(count)

    Gauge(
        "agentpilot_pending_action_oldest_age_seconds",
        "Âge de la plus ancienne action en attente d'un humain, en secondes (0 : aucune).",
        registry=registry,
    ).set(0 if oldest is None else max(0.0, (now - oldest).total_seconds()))

    Gauge(
        "agentpilot_runs_running",
        "Exécutions de tickets en cours dans ce processus.",
        registry=registry,
    ).set(len(status.running_tickets))
    Gauge(
        "agentpilot_runs_max_concurrent",
        "Exécutions simultanées au plus.",
        registry=registry,
    ).set(status.max_concurrent_runs)

    loop = Gauge(
        "agentpilot_loop_state",
        "État de la boucle de traitement : la série de l'état courant vaut 1, les autres 0.",
        ["state"],
        registry=registry,
    )
    current = loop_state(status, now)
    for state in LOOP_STATES:
        loop.labels(state=state).set(1 if state == current else 0)

    Gauge(
        "agentpilot_emergency_stop",
        "1 si l'arrêt d'urgence est actif (toutes les actions sont refusées).",
        registry=registry,
    ).set(1 if status.emergency_stop else 0)
    Gauge(
        "agentpilot_budget_spent_today_usd",
        "Dépense du jour (UTC), coûts inconnus comptés au plafond d'une exécution, en dollars.",
        registry=registry,
    ).set(float(status.spent_today_usd))
    Gauge(
        "agentpilot_budget_daily_usd",
        "Plafond de dépense par jour, en dollars.",
        registry=registry,
    ).set(float(status.daily_budget_usd))
    Gauge(
        "agentpilot_abandoned_tickets",
        "Tickets ouverts abandonnés après trop d'échecs.",
        registry=registry,
    ).set(len(status.abandoned_tickets))

    # Dernier relevé de la file : absent tant que la boucle ne l'a pas relevée
    if status.open_tickets is not None and status.eligible_tickets is not None:
        queue = Gauge(
            "agentpilot_queue_tickets",
            "Dernier relevé de la file : tickets ouverts dans ShopSim, et éligibles.",
            ["state"],
            registry=registry,
        )
        queue.labels(state="open").set(status.open_tickets)
        queue.labels(state="eligible").set(status.eligible_tickets)
        skipped = Gauge(
            "agentpilot_queue_skipped_tickets",
            "Dernier relevé de la file : tickets ouverts écartés, par motif.",
            ["reason"],
            registry=registry,
        )
        for reason, count in status.skipped_tickets.items():
            skipped.labels(reason=reason).set(count)
    return registry
