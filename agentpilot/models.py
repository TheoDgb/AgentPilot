"""Exécutions d'agents enregistrées par AgentPilot (schéma `agentpilot`) : exécutions,
journal des appels d'outils, actions en attente de validation humaine et interrupteurs de
pilotage (boucle de traitement, arrêt d'urgence)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentpilot.db import SCHEMA, Base
from observability.trace import new_trace_id


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Service arrêté pendant l'exécution : le ticket est relancé par la boucle
    INTERRUPTED = "interrupted"


class RunTrigger(StrEnum):
    # `POST /runs/...` ou vérification
    MANUAL = "manual"
    # Boucle de traitement de la file (`agentpilot/worker.py`)
    LOOP = "loop"


class PauseReason(StrEnum):
    MANUAL = "manual"
    # Plafond de dépense du jour atteint : pause automatique
    BUDGET = "budget"


class AgentKind(StrEnum):
    SUPPORT = "support"
    # Vérification de l'isolement de l'agent Support (consigne, politique, outils)
    SUPPORT_CHECK = "support_check"


class CallOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Refusé par le hook et mis en attente de validation humaine
    HELD = "held"
    # Refusé par le hook (autre ticket, autre client, vérification impossible)
    DENIED = "denied"


class PendingStatus(StrEnum):
    PENDING = "pending"
    # Approuvée : exécution en cours, ou à relancer après une panne de ShopSim
    APPROVED = "approved"
    EXECUTED = "executed"
    # Approuvée mais devenue impossible (ticket ou commande changés) : rien n'a été exécuté
    FAILED = "failed"
    REJECTED = "rejected"


def _enum(enum: type[StrEnum], name: str) -> Enum:
    return Enum(enum, name=name, schema=SCHEMA, values_callable=lambda e: [m.value for m in e])


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        # Jamais deux exécutions en cours sur un même ticket, quel que soit le déclencheur
        Index(
            "one_running_run_per_ticket",
            "ticket_id",
            unique=True,
            postgresql_where=text("status = 'running' AND agent = 'support'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent: Mapped[AgentKind] = mapped_column(_enum(AgentKind, "agent_kind"))
    ticket_id: Mapped[int | None] = mapped_column(index=True)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), index=True)
    trigger: Mapped[RunTrigger] = mapped_column(
        _enum(RunTrigger, "run_trigger"), default=RunTrigger.MANUAL
    )
    # Arrêt d'urgence : au moins une action refusée, la décision n'est qu'une proposition
    propose_only: Mapped[bool] = mapped_column(default=False)
    # Trace de l'exécution dans les logs des trois services (`observability.trace`)
    trace_id: Mapped[str] = mapped_column(String(32), default=new_trace_id, index=True)
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
    # Outils réellement disponibles (événement d'initialisation)
    available_tools: Mapped[list[str] | None] = mapped_column(JSONB)
    # Heure réelle : horloge de la plateforme, pas celle de la simulation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tool_calls: Mapped[list["ToolCall"]] = relationship(
        order_by="ToolCall.seq", viewonly=True, lazy="selectin"
    )
    pending_actions: Mapped[list["PendingAction"]] = relationship(
        order_by="PendingAction.created_at", viewonly=True, lazy="selectin"
    )


class PendingAction(Base):
    """Action sensible arrêtée par le hook, en attente d'un humain, puis sa décision.

    De quoi l'exécuter plus tard à l'identique : l'outil, les paramètres exacts de l'agent
    et la clé d'idempotence que le serveur MCP aurait envoyée à ShopSim.
    """

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.runs.id"), index=True)
    ticket_id: Mapped[int] = mapped_column(index=True)
    # Nom court de l'outil MCP : refund_order ou resend_order
    tool: Mapped[str] = mapped_column(String(100))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Raisons de la mise en attente : over_threshold, risky_customer
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    # Ce que le hook a lu lui-même dans ShopSim pour décider
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[PendingStatus] = mapped_column(
        _enum(PendingStatus, "pending_status"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Décision humaine (nom déclaratif : pas d'authentification)
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Motif : obligatoire pour un refus
    decision_note: Mapped[str | None] = mapped_column(Text)
    # Exécution par la plateforme : réponse de ShopSim, ou motif de l'échec
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # Suite donnée au client par l'humain
    customer_message: Mapped[str | None] = mapped_column(Text)
    message_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Trace de la dernière opération de la plateforme sur ShopSim pour cette action
    # (exécution, relance, message au client) ; celle de l'agent est sur son exécution
    trace_id: Mapped[str | None] = mapped_column(String(32))

    run: Mapped[Run] = relationship(viewonly=True, lazy="joined")


class ToolCall(Base):
    """Journal de l'exécution : un appel d'outil, écrit dès qu'il a eu lieu."""

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.runs.id"), index=True)
    # Ordre des appels dans l'exécution
    seq: Mapped[int]
    tool_use_id: Mapped[str | None] = mapped_column(String(100))
    tool: Mapped[str] = mapped_column(String(200))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[CallOutcome] = mapped_column(_enum(CallOutcome, "call_outcome"))
    # Réponse de l'outil, tronquée ; message du hook ou de l'outil en cas de refus ou d'échec
    response: Mapped[Any] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]
    pending_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.pending_actions.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


CONTROL_ID = 1


class Control(Base):
    """Interrupteurs de pilotage, sur une seule ligne (`id = 1`), créée par `init_db`.

    Les noms sont déclaratifs (pas d'authentification). Au premier démarrage, la boucle est en
    pause : rien n'est dépensé tant qu'un humain ne l'a pas lancée.
    """

    __tablename__ = "control"

    id: Mapped[int] = mapped_column(primary_key=True)
    loop_paused: Mapped[bool] = mapped_column(default=True)
    pause_reason: Mapped[PauseReason | None] = mapped_column(_enum(PauseReason, "pause_reason"))
    paused_by: Mapped[str | None] = mapped_column(String(100))
    # Dernier changement de la boucle (pause ou relance)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Les hooks refusent toute action : l'agent ne fait que proposer
    emergency_stop: Mapped[bool] = mapped_column(default=False)
    emergency_by: Mapped[str | None] = mapped_column(String(100))
    emergency_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
