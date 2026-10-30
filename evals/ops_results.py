"""Résultats d'une évaluation de l'agent Ops (`evals.ops_run`), lus par `evals.ops_grade`.

Un fichier par passage, `evals/results/ops-<date>-<nom>[-pN].json` (non versionné), réécrit
après chaque scénario. Il garde, pour chaque scénario, ce qui s'est passé (pannes, alerte,
enquête) et ce qu'il faut pour noter sans rien relancer : le rapport et les appels d'outils de
l'enquête avec leurs réponses.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ScenarioStatus(StrEnum):
    # Enquête terminée, rapport validé : noté
    COMPLETED = "completed"
    # Enquête en échec du fait de l'agent (rapport invalide, délai…) : noté, en échec
    FAILED = "failed"
    # L'alerte attendue n'est pas venue à temps : environnement, hors du score
    NO_ALERT = "no_alert"
    # Erreur d'infrastructure (API, service) ou arrêt pendant le scénario : hors du score
    INTERRUPTED = "interrupted"
    # Pas lancé (budget, arrêt avant)
    NOT_RUN = "not_run"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


class FaultWindow(BaseModel):
    fault: str
    started_at: datetime
    stopped_at: datetime | None = None


class ScenarioResult(BaseModel):
    scenario_id: str
    status: ScenarioStatus
    reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    faults: list[FaultWindow] = Field(default_factory=list)
    refunds_sent: int = 0
    alert_id: uuid.UUID | None = None
    alert_started_at: datetime | None = None
    run_id: uuid.UUID | None = None
    trace_id: str | None = None
    run_status: str | None = None
    error: str | None = None
    error_type: str | None = None
    report: dict[str, Any] | None = None
    # Appels d'outils de l'enquête, réponses comprises (`tool_calls` de `GET /runs/{id}`)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    model: str | None = None


class OpsEvalSummary(BaseModel):
    name: str
    series: str
    pass_index: int = 1
    passes: int = 1
    model_requested: str
    models_used: list[str] = Field(default_factory=list)
    commit: str | None = None
    dirty: bool = False
    # Empreintes de la consigne de l'agent Ops (`agents/ops/`) et de la vérité terrain
    prompt_sha256: str
    truth_sha256: str
    scenarios_requested: list[str]
    max_budget_usd: float
    total_cost_usd: float = 0.0
    status: RunStatus = RunStatus.INTERRUPTED
    stop_reason: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class OpsEvalReport(BaseModel):
    summary: OpsEvalSummary
    scenarios: list[ScenarioResult] = Field(default_factory=list)
