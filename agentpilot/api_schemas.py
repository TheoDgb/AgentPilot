"""Schémas Pydantic des réponses de l'API AgentPilot."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentpilot.models import (
    AgentKind,
    AlertStatus,
    CallOutcome,
    PauseReason,
    PendingStatus,
    RunStatus,
    RunTrigger,
)


class RunCreated(BaseModel):
    id: uuid.UUID
    status: RunStatus
    # Pour retrouver l'exécution dans les logs : `python -m observability.query --trace <id>`
    trace_id: str


class ToolCallRead(BaseModel):
    """Appel d'outil inscrit au journal par les hooks."""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    tool: str
    input: dict[str, Any]
    outcome: CallOutcome
    response: Any
    error: str | None
    duration_ms: int | None
    pending_action_id: uuid.UUID | None


class PendingActionRead(BaseModel):
    """Action sensible arrêtée par le hook, et la décision humaine qui a suivi."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: int
    tool: str
    params: dict[str, Any]
    reasons: list[str]
    facts: dict[str, Any]
    idempotency_key: str
    status: PendingStatus
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    executed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    customer_message: str | None
    message_sent_at: datetime | None
    resolved_at: datetime | None
    trace_id: str | None


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent: AgentKind
    ticket_id: int | None
    status: RunStatus
    trigger: RunTrigger
    trace_id: str
    # Arrêt d'urgence pendant l'exécution : actions refusées, décision = proposition
    propose_only: bool
    simulation_date: date | None
    model: str | None
    # SupportDecision validée (agent Support) ou réponse de vérification (check)
    output: dict[str, Any] | None
    error: str | None
    total_cost_usd: Decimal | None
    duration_ms: int | None
    num_turns: int | None
    session_id: str | None
    available_tools: list[str] | None
    tool_calls: list[ToolCallRead]
    pending_actions: list[PendingActionRead]
    created_at: datetime
    finished_at: datetime | None


class AlertRead(BaseModel):
    """Alerte reçue d'Alertmanager (J4.4)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alertname: str
    severity: str | None
    route: str | None
    status: AlertStatus
    summary: str | None
    starts_at: datetime
    ends_at: datetime | None
    first_received_at: datetime
    last_received_at: datetime


class ControlStatus(BaseModel):
    """Pilotage : boucle, arrêt d'urgence, budget du jour (UTC) et santé de ShopSim."""

    loop_paused: bool
    pause_reason: PauseReason | None
    paused_by: str | None
    paused_at: datetime | None
    emergency_stop: bool
    emergency_by: str | None
    emergency_at: datetime | None
    spent_today_usd: Decimal
    daily_budget_usd: Decimal
    running_tickets: list[int]
    max_concurrent_runs: int
    # Attente en cours : ShopSim en panne ou exécutions en échec d'affilée
    waiting_until: datetime | None
    waiting_reason: str | None
    abandoned_tickets: list[int]
    # Dernier relevé de la file (absent tant que la boucle n'a pas relevé la file)
    queue_checked_at: datetime | None
    open_tickets: int | None
    eligible_tickets: int | None
    # Tickets ouverts écartés, par motif : running, held, abandoned, handled, proposal, cooldown
    skipped_tickets: dict[str, int]
    # Alertes en cours (déclenchées, pas encore résolues), les plus anciennes d'abord
    active_alerts: list[AlertRead] = []
