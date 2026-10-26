"""Schémas Pydantic des réponses de l'API AgentPilot."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentpilot.models import AgentKind, CallOutcome, PendingStatus, RunStatus


class RunCreated(BaseModel):
    id: uuid.UUID
    status: RunStatus


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
    """Action sensible arrêtée par le hook, en attente d'un humain."""

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


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent: AgentKind
    ticket_id: int | None
    status: RunStatus
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
