"""Enquête automatique de l'agent Ops sur les alertes (J6.5) : tri des alertes reçues.

Appelé à chaque tour de la boucle (`Worker.triage_alerts`), avant les tickets, seulement si
l'interrupteur `control.ops_auto` est actif. Chaque alerte est triée **une seule fois**
(`alerts.triage`, écrit sous condition `triage IS NULL`) :

- `skipped` : déjà résolue au moment du tri ;
- `excluded` : hors de la liste blanche (`INVESTIGATED_ALERTS`), avec son motif ;
- `investigated` ou `degraded` : alerte de tête de son groupe, enquête de l'agent Ops ou
  rapport sans IA (`agentpilot/ops_degraded.py`) ;
- `covered` : même panne qu'une alerte de tête (`covered_by`).

**Regroupement** (une panne, une enquête) : les alertes de même nom nées à moins de
`GROUP_WINDOW` de l'alerte de tête forment un groupe ; la plus ancienne en est la tête. Une
alerte n'est triée que `GROUP_WAIT` après sa réception, le temps que ses voisines arrivent (les
5 alertes d'une saturation naissent en moins de 30 s). Deux noms différents (erreurs et
lenteur) font deux enquêtes ; deux pannes simultanées de même nom d'alerte sont regroupées
(limite connue, l'agent voit toutes les alertes actives).

Ce module ne fait que décider (`plan`, sans base ni horloge) et écrire le tri (`claim`) ;
le lancement (places, budget, mode dégradé) est dans `Worker`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.env_file import setting
from agentpilot.models import AgentKind, Alert, AlertStatus, Run, RunTrigger, Triage

# Alertes qui déclenchent une enquête : symptômes de la boutique seulement. Liste blanche : une
# nouvelle règle d'alerte ne dépense rien tant qu'elle n'y est pas
INVESTIGATED_ALERTS = frozenset({"ShopSimHighErrorRate", "ShopSimHighLatency", "ShopSimDown"})
# Alertes connues jamais enquêtées, et pourquoi
EXCLUDED_REASONS = {
    "AgentPilotDown": (
        "plateforme injoignable : l'enquête tournerait sur la plateforme en panne (l'alerte "
        "n'arrive d'ailleurs qu'à sa résolution)"
    ),
    "AgentPilotStateUnavailable": "base illisible : l'enquête ne pourrait pas être enregistrée",
    "AgentBudgetNearlyReached": "budget presque atteint : une enquête aggraverait le problème",
    "AgentRunsFailing": "l'IA échoue déjà : l'enquête échouerait probablement aussi",
    "PendingActionTooOld": "décision humaine en attente, pas une panne",
    "OpsReportWithoutAI": "signale un rapport sans IA : une enquête ferait une boucle",
}
UNKNOWN_REASON = "règle d'alerte pas encore autorisée pour l'enquête automatique"

GROUP_WAIT = timedelta(seconds=60)
GROUP_WINDOW = timedelta(minutes=5)
MAX_PER_HOUR_ENV = "AGENTPILOT_OPS_MAX_PER_HOUR"
DEFAULT_MAX_PER_HOUR = 6


def max_per_hour() -> int:
    """Enquêtes automatiques par heure glissante, au plus (environnement, sinon `.env`)."""
    value = setting(MAX_PER_HOUR_ENV)
    return int(value) if value else DEFAULT_MAX_PER_HOUR


@dataclass(frozen=True)
class AlertRow:
    id: uuid.UUID
    alertname: str
    status: AlertStatus
    starts_at: datetime
    first_received_at: datetime


@dataclass
class Group:
    """Une alerte de tête à lancer, et les alertes qu'elle couvre."""

    head: AlertRow
    covered: list[AlertRow] = field(default_factory=list)


@dataclass
class Plan:
    skipped: list[AlertRow] = field(default_factory=list)
    excluded: list[tuple[AlertRow, str]] = field(default_factory=list)
    # Couvertes par une alerte de tête déjà lancée : (alerte, tête)
    covered: list[tuple[AlertRow, uuid.UUID]] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    # Prochaine alerte prête (fin de son délai de regroupement), pour le tour suivant
    next_ready: datetime | None = None


def _same_group(alert: AlertRow, head: AlertRow) -> bool:
    return alert.alertname == head.alertname and abs(alert.starts_at - head.starts_at) <= (
        GROUP_WINDOW
    )


def plan(untriaged: Sequence[AlertRow], heads: Sequence[AlertRow], now: datetime) -> Plan:
    """Tri des alertes pas encore triées ; `heads` : alertes de tête déjà lancées (enquête ou
    rapport sans IA). Une alerte encore dans son délai de regroupement attend, et retient les
    alertes de même nom plus récentes qu'elle (sinon l'une d'elles deviendrait la tête)."""
    found = Plan()
    known = list(heads)
    waiting: list[AlertRow] = []
    for alert in sorted(untriaged, key=lambda a: (a.starts_at, a.alertname, str(a.id))):
        if alert.status is AlertStatus.RESOLVED:
            found.skipped.append(alert)
            continue
        if alert.alertname not in INVESTIGATED_ALERTS:
            found.excluded.append((alert, EXCLUDED_REASONS.get(alert.alertname, UNKNOWN_REASON)))
            continue
        head = next((h for h in known if _same_group(alert, h)), None)
        if head is not None:
            found.covered.append((alert, head.id))
            continue
        group = next((g for g in found.groups if _same_group(alert, g.head)), None)
        if group is not None:
            group.covered.append(alert)
            continue
        ready_at = alert.first_received_at + GROUP_WAIT
        if now < ready_at:
            waiting.append(alert)
            found.next_ready = min(found.next_ready or ready_at, ready_at)
            continue
        if any(_same_group(alert, w) for w in waiting):
            # Retenue par une voisine plus ancienne encore dans son délai
            waiting.append(alert)
            continue
        found.groups.append(Group(alert))
    return found


# --- Base ------------------------------------------------------------------------------------


def _row(alert: Alert) -> AlertRow:
    return AlertRow(
        alert.id, alert.alertname, alert.status, alert.starts_at, alert.first_received_at
    )


def load(factory: sessionmaker[Session]) -> tuple[list[AlertRow], list[AlertRow]]:
    """Alertes pas encore triées, et alertes de tête des `GROUP_WINDOW` dernières minutes (et
    plus : une alerte née avant peut encore avoir des voisines non triées)."""
    with factory() as session:
        untriaged = session.scalars(select(Alert).where(Alert.triage.is_(None))).all()
        if not untriaged:
            return [], []
        oldest = min(a.starts_at for a in untriaged) - GROUP_WINDOW
        heads = session.scalars(
            select(Alert).where(
                Alert.triage.in_([Triage.INVESTIGATED, Triage.DEGRADED]),
                Alert.starts_at >= oldest,
            )
        ).all()
        return [_row(a) for a in untriaged], [_row(a) for a in heads]


def claim(
    factory: sessionmaker[Session],
    alert_id: uuid.UUID,
    triage: Triage,
    detail: str | None,
    now: datetime,
    *,
    run_id: uuid.UUID | None = None,
    covered_by: uuid.UUID | None = None,
) -> bool:
    """Écrit le tri d'une alerte pas encore triée ; False si elle l'était déjà."""
    with factory() as session:
        done = session.execute(
            update(Alert)
            .where(Alert.id == alert_id, Alert.triage.is_(None))
            .values(
                triage=triage,
                triage_detail=detail,
                triage_run_id=run_id,
                covered_by=covered_by,
                triaged_at=now,
            )
        )
        session.commit()
        return bool(done.rowcount)  # type: ignore[attr-defined]


def retriage(
    factory: sessionmaker[Session],
    alert_id: uuid.UUID,
    triage: Triage,
    detail: str,
    run_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Change le tri d'une alerte de tête : enquête lancée (son exécution), ou enquête par l'IA
    arrêtée sur une erreur d'infrastructure, remplacée par un rapport sans IA."""
    with factory() as session:
        session.execute(
            update(Alert)
            .where(Alert.id == alert_id)
            .values(triage=triage, triage_detail=detail, triage_run_id=run_id, triaged_at=now)
        )
        session.commit()


def launched_last_hour(factory: sessionmaker[Session], now: datetime) -> int:
    """Enquêtes par l'IA lancées par une alerte dans l'heure glissante."""
    with factory() as session:
        return int(
            session.scalar(
                select(func.count()).where(
                    Run.agent == AgentKind.OPS,
                    Run.trigger == RunTrigger.ALERT,
                    Run.created_at >= now - timedelta(hours=1),
                )
            )
            or 0
        )
