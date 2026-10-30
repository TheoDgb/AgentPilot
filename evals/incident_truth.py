"""Vérité terrain des incidents (J6.3) : `evals/incidents.jsonl`, un scénario par ligne.

Chaque scénario dit quelles pannes activer et quand, quelle(s) alerte(s) confier à l'agent
Ops (J6.4 : plusieurs enquêtes par scénario quand plusieurs pannes sont actives), et ce qu'il
doit conclure pour chacune (cause sur la liste fermée de `IncidentReport`, routes touchées).

Ce fichier n'est lu que par l'évaluation (`evals.ops_run`, `evals.ops_grade`), jamais par
l'agent Ops : ni ses outils (logs sur liste fixe, adresses fixes) ni sa consigne n'y mènent
(tests dans `evals/tests/test_incident_truth.py`).
"""

import hashlib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentpilot.ops_schemas import IncidentCause
from shopsim.app.models import ChaosScenario

INCIDENTS_PATH = Path(__file__).parent / "incidents.jsonl"

_ORDERS = ("/orders/{order_id}", "/customers/{customer_id}/orders")
# Routes où chaque panne se voit sous le trafic de l'évaluation (générateur et charge de
# remboursements) : sert à dire, dans la notation, qu'une route citée en trop appartient à
# une autre panne du scénario
FAULT_ROUTES: dict[ChaosScenario, frozenset[str]] = {
    ChaosScenario.DB_ERRORS: frozenset(_ORDERS),
    ChaosScenario.DB_ERRORS_INTERMITTENT: frozenset(_ORDERS),
    ChaosScenario.PAYMENT_LATENCY: frozenset({"/payments/{order_id}"}),
    ChaosScenario.BAD_DEPLOY: frozenset({"/refunds"}),
    ChaosScenario.CONNECTION_SATURATION: frozenset(
        {*_ORDERS, "/customers/{customer_id}", "/payments/{order_id}", "/shipments/{order_id}"}
    ),
}


class Step(BaseModel):
    """Une panne, active de `start_min` à `stop_min` (minutes depuis le départ du scénario ;
    sans fin : jusqu'à la fin du scénario)."""

    model_config = ConfigDict(extra="forbid")

    fault: ChaosScenario
    start_min: float = Field(ge=0)
    stop_min: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.stop_min is not None and self.stop_min <= self.start_min:
            raise ValueError(f"{self.fault.value} : fin avant le début")
        return self


class RefundLoad(BaseModel):
    """Remboursements envoyés par l'évaluation pendant une panne (jamais par un agent)."""

    model_config = ConfigDict(extra="forbid")

    per_minute: int = Field(ge=1, le=60)
    during_step: int = Field(ge=0, description="Index de l'étape pendant laquelle envoyer")


class InjectedAlert(BaseModel):
    """Fausse alerte postée sur le webhook d'AgentPilot, comme l'enverrait Alertmanager."""

    model_config = ConfigDict(extra="forbid")

    alertname: str
    route: str | None
    severity: str
    summary: str


class AlertTarget(BaseModel):
    """L'alerte à confier à l'agent. `started_before_step` : l'occurrence née avant le début de
    cette étape (enchaînement : l'alerte qui survit au changement de cause)."""

    model_config = ConfigDict(extra="forbid")

    alertname: str
    route: str | None
    started_before_step: int | None = None


class Investigation(BaseModel):
    """Une alerte confiée à l'agent, et ce qu'il doit conclure pour elle. Quand plusieurs
    pannes sont actives, chaque alerte a sa cause et ses routes : une route d'une autre panne
    n'est ni attendue ni admise, donc interdite."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    alert: AlertTarget
    investigate_after_min: float = Field(ge=0)
    expected_cause: IncidentCause
    expected_routes: list[str] = Field(default_factory=list)
    allowed_routes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expected_cause is IncidentCause.NO_INCIDENT and self.expected_routes:
            raise ValueError("aucune panne attendue : aucune route attendue")
        if set(self.expected_routes) & set(self.allowed_routes):
            raise ValueError("une route est à la fois attendue et admise")
        return self


class IncidentScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str
    history_min: float = Field(ge=0, description="Trafic normal exigé avant le départ")
    steps: list[Step] = Field(default_factory=list)
    load: RefundLoad | None = None
    inject_alert: InjectedAlert | None = None
    investigations: list[Investigation] = Field(min_length=1)
    alert_timeout_min: float = Field(gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        starts = [step.start_min for step in self.steps]
        if starts != sorted(starts):
            raise ValueError("étapes dans le désordre")
        if not self.steps and self.inject_alert is None:
            raise ValueError("ni panne ni fausse alerte : aucune alerte ne viendrait")
        if self.load is not None and self.load.during_step >= len(self.steps):
            raise ValueError("charge sur une étape inexistante")
        for investigation in self.investigations:
            before = investigation.alert.started_before_step
            if before is not None and not 0 < before < len(self.steps):
                raise ValueError("started_before_step : étape inexistante ou première")
        names = [i.name for i in self.investigations]
        if len(names) > 1 and (None in names or len(set(names)) != len(names)):
            raise ValueError("plusieurs enquêtes : chacune a un nom, tous différents")
        targets = [(i.alert.alertname, i.alert.route) for i in self.investigations]
        if len(set(targets)) != len(targets):
            raise ValueError("une même alerte visée deux fois")
        return self

    def fault_routes(self) -> frozenset[str]:
        """Routes touchées par l'une des pannes du scénario."""
        return frozenset().union(*(FAULT_ROUTES[step.fault] for step in self.steps))

    def investigation(self, name: str | None) -> Investigation:
        return next(i for i in self.investigations if i.name == name)

    def end_min(self) -> float:
        """Durée prévue des pannes (sans l'attente de l'alerte ni l'enquête)."""
        stops = [s.stop_min for s in self.steps if s.stop_min is not None]
        last = max(i.investigate_after_min for i in self.investigations)
        return max([last, *stops])


def load_scenarios(path: Path = INCIDENTS_PATH) -> list[IncidentScenario]:
    scenarios = [
        IncidentScenario.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [s.scenario_id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("deux scénarios portent le même nom")
    return scenarios


def truth_sha256(path: Path = INCIDENTS_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
