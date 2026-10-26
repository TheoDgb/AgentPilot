"""Schémas Pydantic des réponses de l'API AgentPilot."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentpilot.models import AgentKind, RunStatus


class RunCreated(BaseModel):
    id: uuid.UUID
    status: RunStatus


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
    tool_calls: list[dict[str, Any]]
    created_at: datetime
    finished_at: datetime | None
