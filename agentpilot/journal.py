"""Journal d'une exécution et file des actions en attente, écrits par les hooks.

Chaque appel d'outil est écrit dès qu'il a eu lieu : le journal survit à un arrêt du service
en cours d'exécution, et la relance du ticket sait quelles actions ont déjà été faites.

Les hooks y lisent aussi l'arrêt d'urgence, sans cache : basculer l'interrupteur agit dès
l'appel d'outil suivant.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.models import (
    CONTROL_ID,
    CallOutcome,
    Control,
    PendingAction,
    PendingStatus,
    Run,
    ToolCall,
)


@dataclass(frozen=True)
class CallEntry:
    seq: int
    tool_use_id: str | None
    tool: str
    input: dict[str, Any]
    outcome: CallOutcome
    response: Any = None
    error: str | None = None
    duration_ms: int | None = None
    pending_action_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PendingEntry:
    ticket_id: int
    tool: str
    params: dict[str, Any]
    reasons: list[str]
    facts: dict[str, Any]
    idempotency_key: str


class Journal(Protocol):
    """Ce dont les hooks ont besoin ; appels synchrones (lancés hors de la boucle)."""

    def record_call(self, entry: CallEntry) -> None: ...

    def hold(self, entry: PendingEntry) -> tuple[uuid.UUID, bool]:
        """Met l'action en attente ; renvoie son id et False si elle l'était déjà."""
        ...

    def has_pending(self, ticket_id: int) -> bool: ...

    def emergency_stop(self) -> bool:
        """Arrêt d'urgence actif ? Une erreur doit remonter : le hook refuse alors l'action."""
        ...

    def mark_propose_only(self) -> None:
        """Une action a été refusée par l'arrêt d'urgence : la décision n'est qu'une proposition."""
        ...


class DbJournal:
    """Journal en base (schéma `agentpilot`) pour une exécution."""

    def __init__(self, session_factory: sessionmaker[Session], run_id: uuid.UUID) -> None:
        self._factory = session_factory
        self._run_id = run_id
        # Appels parallèles de l'agent : une écriture à la fois
        self._lock = threading.Lock()

    def record_call(self, entry: CallEntry) -> None:
        with self._lock, self._factory() as session:
            session.add(
                ToolCall(
                    run_id=self._run_id,
                    seq=entry.seq,
                    tool_use_id=entry.tool_use_id,
                    tool=entry.tool,
                    input=entry.input,
                    outcome=entry.outcome,
                    response=entry.response,
                    error=entry.error,
                    duration_ms=entry.duration_ms,
                    pending_action_id=entry.pending_action_id,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()

    def hold(self, entry: PendingEntry) -> tuple[uuid.UUID, bool]:
        with self._lock, self._factory() as session:
            existing = session.scalar(
                select(PendingAction).where(PendingAction.idempotency_key == entry.idempotency_key)
            )
            if existing is not None:
                return existing.id, False
            action = PendingAction(
                run_id=self._run_id,
                ticket_id=entry.ticket_id,
                tool=entry.tool,
                params=entry.params,
                reasons=entry.reasons,
                facts=entry.facts,
                idempotency_key=entry.idempotency_key,
                status=PendingStatus.PENDING,
                created_at=datetime.now(UTC),
            )
            session.add(action)
            session.commit()
            return action.id, True

    def has_pending(self, ticket_id: int) -> bool:
        with self._lock, self._factory() as session:
            return bool(
                session.scalar(
                    select(
                        exists().where(
                            PendingAction.ticket_id == ticket_id,
                            PendingAction.status == PendingStatus.PENDING,
                        )
                    )
                )
            )

    def emergency_stop(self) -> bool:
        with self._factory() as session:
            stopped = session.scalar(select(Control.emergency_stop).where(Control.id == CONTROL_ID))
            if stopped is None:
                raise LookupError("interrupteurs de pilotage absents")
            return stopped

    def mark_propose_only(self) -> None:
        with self._lock, self._factory() as session:
            session.execute(update(Run).where(Run.id == self._run_id).values(propose_only=True))
            session.commit()
