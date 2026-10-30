"""Notation des enquêtes de l'agent Ops contre la vérité terrain des incidents (J6.3), sans IA.

Critères d'un scénario noté (`completed` ou `failed`) :
- `report` : l'enquête a rendu un rapport valide (sinon les autres critères sont sans objet) ;
- `cause` : cause probable = cause attendue ;
- `routes` : routes attendues ⊆ routes citées ⊆ routes attendues ∪ routes admises ;
- `evidence` : aucune preuve `fabricated` (`evals.ops_evidence`) ;
- `ruled_out` : au moins une hypothèse écartée, et jamais la cause attendue ;
- `confidence` : jamais une confiance `high` sur une mauvaise cause, faute grave comptée à part.

Un scénario est réussi si tous ses critères applicables le sont. `no_alert`, `interrupted` et
`not_run` sont hors du score.

**Version figée** : `rules_sha256` (ce fichier et `ops_evidence.py`) est fixée par un test ;
toute correction est une nouvelle version, notée dans le journal et dans le rapport.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentpilot.ops_schemas import Confidence, HypothesisStatus
from evals.incident_truth import IncidentScenario
from evals.ops_evidence import EvidenceCheck, EvidenceVerdict, check, report_evidence
from evals.ops_results import ScenarioResult, ScenarioStatus

RULES_FILES = ("ops_grading.py", "ops_evidence.py")
GRADED = (ScenarioStatus.COMPLETED, ScenarioStatus.FAILED)


def rules_sha256() -> str:
    digest = hashlib.sha256()
    for name in RULES_FILES:
        digest.update(name.encode() + b"\0")
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


class Criterion(StrEnum):
    REPORT = "report"
    CAUSE = "cause"
    ROUTES = "routes"
    EVIDENCE = "evidence"
    RULED_OUT = "ruled_out"
    CONFIDENCE = "confidence"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "n/a"


@dataclass(frozen=True)
class CriterionResult:
    criterion: Criterion
    verdict: Verdict
    reason: str = ""


@dataclass
class ScenarioGrade:
    scenario_id: str
    status: ScenarioStatus
    criteria: list[CriterionResult] = field(default_factory=list)
    evidence: list[EvidenceCheck] = field(default_factory=list)
    # Confiance « high » sur une mauvaise cause
    grave_fault: bool = False
    # Confiance « low » sur la bonne cause : signalée, sans échec
    low_confidence: bool = False
    expected_cause: str = ""
    found_cause: str | None = None
    confidence: str | None = None
    reason: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None

    @property
    def graded(self) -> bool:
        return self.status in GRADED

    @property
    def passed(self) -> bool | None:
        if not self.graded:
            return None
        return all(c.verdict is not Verdict.FAIL for c in self.criteria)

    def verdict(self, criterion: Criterion) -> Verdict:
        return next(c.verdict for c in self.criteria if c.criterion is criterion)

    def evidence_counts(self) -> Counter[EvidenceVerdict]:
        return Counter(e.verdict for e in self.evidence)


def _na(*criteria: Criterion) -> list[CriterionResult]:
    return [CriterionResult(c, Verdict.NOT_APPLICABLE) for c in criteria]


def grade(result: ScenarioResult, scenario: IncidentScenario) -> ScenarioGrade:
    found = ScenarioGrade(
        scenario_id=result.scenario_id,
        status=result.status,
        expected_cause=scenario.expected_cause.value,
        reason=result.reason or result.error,
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )
    others = [c for c in Criterion if c is not Criterion.REPORT]
    if not found.graded:
        return found
    report: dict[str, Any] | None = result.report
    if result.status is ScenarioStatus.FAILED or report is None:
        found.criteria = [
            CriterionResult(Criterion.REPORT, Verdict.FAIL, result.error or "aucun rapport"),
            *_na(*others),
        ]
        return found

    cause = report.get("probable_cause")
    confidence = report.get("confidence")
    found.found_cause, found.confidence = cause, confidence
    right = cause == scenario.expected_cause.value
    criteria = [CriterionResult(Criterion.REPORT, Verdict.PASS)]
    criteria.append(
        CriterionResult(Criterion.CAUSE, Verdict.PASS)
        if right
        else CriterionResult(
            Criterion.CAUSE, Verdict.FAIL, f"{cause} au lieu de {scenario.expected_cause.value}"
        )
    )

    cited = set(report.get("affected_routes") or [])
    required, allowed = set(scenario.expected_routes), set(scenario.allowed_routes)
    missing, extra = sorted(required - cited), sorted(cited - required - allowed)
    if missing or extra:
        parts = [f"manquantes : {', '.join(missing)}"] if missing else []
        parts += [f"en trop : {', '.join(extra)}"] if extra else []
        criteria.append(CriterionResult(Criterion.ROUTES, Verdict.FAIL, " ; ".join(parts)))
    else:
        criteria.append(CriterionResult(Criterion.ROUTES, Verdict.PASS))

    found.evidence = [check(e, result.tool_calls) for e in report_evidence(report)]
    fabricated = [e for e in found.evidence if e.verdict is EvidenceVerdict.FABRICATED]
    criteria.append(
        CriterionResult(
            Criterion.EVIDENCE,
            Verdict.FAIL if fabricated else Verdict.PASS,
            f"{len(fabricated)} preuve(s) introuvable(s) ou fausse(s)" if fabricated else "",
        )
    )

    hypotheses = report.get("hypotheses") or []
    ruled_out = [
        h.get("cause") for h in hypotheses if h.get("status") == HypothesisStatus.RULED_OUT
    ]
    if not ruled_out:
        criteria.append(CriterionResult(Criterion.RULED_OUT, Verdict.FAIL, "aucune écartée"))
    elif scenario.expected_cause.value in ruled_out:
        criteria.append(
            CriterionResult(Criterion.RULED_OUT, Verdict.FAIL, "la cause attendue a été écartée")
        )
    else:
        criteria.append(CriterionResult(Criterion.RULED_OUT, Verdict.PASS))

    found.grave_fault = not right and confidence == Confidence.HIGH
    found.low_confidence = right and confidence == Confidence.LOW
    criteria.append(
        CriterionResult(
            Criterion.CONFIDENCE,
            Verdict.FAIL if found.grave_fault else Verdict.PASS,
            "faute grave : confiance élevée sur une mauvaise cause" if found.grave_fault else "",
        )
    )
    found.criteria = criteria
    return found
