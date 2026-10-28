"""Alertes reçues d'Alertmanager (J4.4) : enregistrées en base, sans action automatique.

- `POST /alerts/webhook` : corps webhook d'Alertmanager (version 4). Chaque alerte est écrite
  par `INSERT … ON CONFLICT` sur (empreinte, début) : un rappel ou un rejeu ne crée pas de
  doublon, une résolution ferme la ligne, et une résolution reçue sans déclenchement (AgentPilot
  était arrêté) crée directement la ligne résolue. Une alerte résolue ne redevient jamais
  active par un message arrivé en retard.
- `GET /alerts` : alertes enregistrées, les plus récentes d'abord.

Les règles sont dans `monitoring/prometheus/rules/alerts.yml`. L'agent Ops partira de ces lignes
en J6 ; aucune exécution n'est lancée ici.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.api_schemas import AlertRead
from agentpilot.models import Alert, AlertStatus
from observability import trace
from observability.logs import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])

SUMMARY_MAX = 1_000
LABEL_MAX = 200


class WebhookAlert(BaseModel):
    """Une alerte du corps webhook d'Alertmanager ; champs inconnus ignorés."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    startsAt: datetime  # noqa: N815  (nom imposé par Alertmanager)
    # « 0001-01-01T00:00:00Z » tant que l'alerte est active
    endsAt: datetime | None = None  # noqa: N815
    fingerprint: str = Field(min_length=1, max_length=64)


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str
    status: Literal["firing", "resolved"]
    alerts: list[WebhookAlert] = Field(max_length=1_000)


def _label(value: str | None, maximum: int = LABEL_MAX) -> str | None:
    return value[:maximum] if value else None


def record(session: Session, alert: WebhookAlert, now: datetime, trace_id: str | None) -> None:
    """Écrit une alerte reçue (idempotent)."""
    resolved = alert.status == "resolved"
    values = {
        "fingerprint": alert.fingerprint,
        "alertname": _label(alert.labels.get("alertname")) or "unknown",
        "severity": _label(alert.labels.get("severity"), 20),
        "route": _label(alert.labels.get("route")),
        "status": AlertStatus.RESOLVED if resolved else AlertStatus.FIRING,
        "summary": _label(alert.annotations.get("summary"), SUMMARY_MAX),
        "starts_at": alert.startsAt,
        "ends_at": alert.endsAt if resolved else None,
        "first_received_at": now,
        "last_received_at": now,
        "trace_id": trace_id,
    }
    statement = insert(Alert).values(**values)
    excluded = statement.excluded
    session.execute(
        statement.on_conflict_do_update(
            constraint="one_row_per_occurrence",
            set_={
                "last_received_at": now,
                "trace_id": trace_id,
                # Résolue reste résolue, même si un rappel arrive après la résolution
                "status": case(
                    (Alert.status == AlertStatus.RESOLVED, Alert.status), else_=excluded.status
                ),
                "ends_at": func.coalesce(Alert.ends_at, excluded.ends_at),
                "summary": excluded.summary,
            },
        )
    )


def active(session: Session) -> list[Alert]:
    return list(
        session.scalars(
            select(Alert).where(Alert.status == AlertStatus.FIRING).order_by(Alert.starts_at)
        )
    )


def _factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


@router.post("/webhook")
def receive(webhook: AlertmanagerWebhook, request: Request) -> dict[str, int]:
    """Alertes d'Alertmanager : enregistrées, sans action automatique."""
    now = datetime.now(UTC)
    trace_id = trace.current_trace_id()
    with _factory(request)() as session:
        for alert in webhook.alerts:
            record(session, alert, now, trace_id)
        session.commit()
    for alert in webhook.alerts:
        log_event(
            logger,
            "alert.received",
            logging.WARNING if alert.status == "firing" else logging.INFO,
            alertname=_label(alert.labels.get("alertname")),
            severity=_label(alert.labels.get("severity"), 20),
            route=_label(alert.labels.get("route")),
            status=alert.status,
        )
    return {"received": len(webhook.alerts)}


@router.get("", response_model=list[AlertRead])
def list_alerts(
    request: Request,
    status: AlertStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Alert]:
    query = select(Alert).order_by(Alert.starts_at.desc()).limit(limit)
    if status is not None:
        query = query.where(Alert.status == status)
    with _factory(request)() as session:
        return list(session.scalars(query))
