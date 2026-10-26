"""Exécutions d'agents enregistrées par AgentPilot (schéma `agentpilot`)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, DateTime, Enum, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentpilot.db import SCHEMA, Base


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Service arrêté pendant l'exécution (reprise en J3.5)
    INTERRUPTED = "interrupted"


class AgentKind(StrEnum):
    SUPPORT = "support"
    # Vérification de l'isolement de l'agent Support (consigne, politique, outils)
    SUPPORT_CHECK = "support_check"


def _enum(enum: type[StrEnum], name: str) -> Enum:
    return Enum(enum, name=name, schema=SCHEMA, values_callable=lambda e: [m.value for m in e])


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent: Mapped[AgentKind] = mapped_column(_enum(AgentKind, "agent_kind"))
    ticket_id: Mapped[int | None] = mapped_column(index=True)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), index=True)
    # Date de l'horloge de ShopSim au lancement (celle donnée à l'agent)
    simulation_date: Mapped[date | None] = mapped_column(Date)
    model: Mapped[str | None] = mapped_column(String(100))
    # Décision validée (SupportDecision) ou réponse de vérification (ContextCheck)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None]
    num_turns: Mapped[int | None]
    session_id: Mapped[str | None] = mapped_column(String(100))
    # Outils réellement disponibles (événement d'initialisation) et outils appelés, dans l'ordre
    available_tools: Mapped[list[str] | None] = mapped_column(JSONB)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Heure réelle : horloge de la plateforme, pas celle de la simulation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
