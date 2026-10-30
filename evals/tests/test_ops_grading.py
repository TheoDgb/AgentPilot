"""Notation de l'agent Ops (`evals.ops_grading`), rapport et commande `evals.ops_grade`."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals import ops_grade, ops_grading
from evals.incident_truth import INCIDENTS_PATH, load_scenarios, truth_sha256
from evals.ops_evidence import EvidenceVerdict
from evals.ops_grading import grade
from evals.ops_results import (
    OpsEvalReport,
    OpsEvalSummary,
    RunStatus,
    ScenarioResult,
    ScenarioStatus,
)
from evals.report import DOLLAR

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ops_run_payment_latency.json").read_text(
        encoding="utf-8"
    )
)
SCENARIOS = {s.scenario_id: s for s in load_scenarios()}
PAYMENTS = SCENARIOS["payment_latency"]

# Version figée de la notation de l'agent Ops : toute correction des règles est une nouvelle
# version, déclarée ici et notée dans le journal. Versions :
# - a10b81ce62bc : première version (J6.3)
# - 11a2215b0643 : J6.4, mêmes critères, attentes par enquête (plusieurs alertes par scénario),
#   route d'une autre panne signalée dans le motif
FROZEN_RULES_SHA256 = "11a2215b0643d81b7d50c054963ee2294db409ebcf2f5665614c75e63d968193"


def result(**changes: Any) -> ScenarioResult:  # noqa: ANN401
    """Le résultat réel ; `report` : champs du rapport à changer, ou None pour aucun rapport."""
    report: dict[str, Any] | None = copy.deepcopy(FIXTURE["output"])
    replaced = changes.pop("report", {})
    if replaced is None:
        report = None
    else:
        report.update(replaced)  # type: ignore[union-attr]
    fields: dict[str, Any] = {
        "scenario_id": "payment_latency",
        "status": ScenarioStatus.COMPLETED,
        "report": report,
        "tool_calls": copy.deepcopy(FIXTURE["tool_calls"]),
        "cost_usd": 0.068154,
        "duration_ms": 19023,
    }
    return ScenarioResult(**(fields | changes))


def verdicts(scenario_grade: ops_grading.ScenarioGrade) -> dict[str, str]:
    return {c.criterion.value: c.verdict.value for c in scenario_grade.criteria}


def test_real_investigation_passes_every_criterion() -> None:
    found = grade(result(), PAYMENTS)

    assert found.passed is True
    assert set(verdicts(found).values()) == {"pass"}
    assert found.evidence_counts() == {EvidenceVerdict.EXACT: 11}
    assert not found.grave_fault and not found.low_confidence


def test_wrong_cause_with_high_confidence_is_a_grave_fault() -> None:
    wrong = {"probable_cause": "resource_saturation", "confidence": "high"}
    wrong["hypotheses"] = [
        {**h, "status": "confirmed" if h["cause"] == "resource_saturation" else "ruled_out"}
        for h in FIXTURE["output"]["hypotheses"]
    ]

    found = grade(result(report=wrong), PAYMENTS)

    assert found.passed is False and found.grave_fault
    assert verdicts(found)["cause"] == "fail" and verdicts(found)["confidence"] == "fail"
    # La cause attendue a été écartée
    assert verdicts(found)["ruled_out"] == "fail"


def test_wrong_cause_with_medium_confidence_is_an_ordinary_failure() -> None:
    found = grade(result(report={"probable_cause": "application_error"}), PAYMENTS)

    assert found.passed is False and not found.grave_fault
    assert verdicts(found)["confidence"] == "pass"


def test_right_cause_with_low_confidence_is_reported_without_failure() -> None:
    found = grade(result(report={"confidence": "low"}), PAYMENTS)

    assert found.passed is True and found.low_confidence


@pytest.mark.parametrize(
    ("routes", "outcome"),
    [
        (["/payments/{order_id}"], "pass"),
        ([], "fail"),
        (["/payments/{order_id}", "/orders/{order_id}"], "fail"),
    ],
)
def test_routes(routes: list[str], outcome: str) -> None:
    assert (
        verdicts(grade(result(report={"affected_routes": routes}), PAYMENTS))["routes"] == outcome
    )


def test_allowed_routes_may_be_cited() -> None:
    investigation = PAYMENTS.investigations[0].model_copy(
        update={"allowed_routes": ["/orders/{order_id}"]}
    )
    scenario = PAYMENTS.model_copy(update={"investigations": [investigation]})
    report = {"affected_routes": ["/payments/{order_id}", "/orders/{order_id}"]}

    assert verdicts(grade(result(report=report), scenario))["routes"] == "pass"


def test_one_fabricated_evidence_fails_the_criterion() -> None:
    report = copy.deepcopy(FIXTURE["output"])
    report["symptoms"][0]["evidence"][0]["value"] = 4999

    found = grade(result(report=report), PAYMENTS)

    assert verdicts(found)["evidence"] == "fail" and found.passed is False
    assert found.evidence_counts()[EvidenceVerdict.FABRICATED] == 1


def test_no_ruled_out_hypothesis_fails() -> None:
    hypotheses = [{**h, "status": "inconclusive"} for h in FIXTURE["output"]["hypotheses"][1:]]
    report = {"hypotheses": [FIXTURE["output"]["hypotheses"][0], *hypotheses]}

    assert verdicts(grade(result(report=report), PAYMENTS))["ruled_out"] == "fail"


def test_failed_investigation_fails_on_the_report_only() -> None:
    failed = result(status=ScenarioStatus.FAILED, report=None, error="Rapport invalide")

    found = grade(failed, PAYMENTS)

    assert found.passed is False
    assert verdicts(found) == {
        "report": "fail",
        "cause": "n/a",
        "routes": "n/a",
        "evidence": "n/a",
        "ruled_out": "n/a",
        "confidence": "n/a",
    }


@pytest.mark.parametrize(
    "status", [ScenarioStatus.NO_ALERT, ScenarioStatus.INTERRUPTED, ScenarioStatus.NOT_RUN]
)
def test_environment_problems_are_never_graded(status: ScenarioStatus) -> None:
    found = grade(result(status=status, report=None), PAYMENTS)

    assert found.passed is None and found.criteria == []


def test_no_incident_expects_no_route() -> None:
    false_alert = SCENARIOS["false_alert"]
    report = copy.deepcopy(FIXTURE["output"])
    report.update(probable_cause="no_incident", affected_routes=[], confidence="high")
    report["hypotheses"][0]["status"] = "ruled_out"
    report["hypotheses"].append(
        {"cause": "no_incident", "status": "confirmed", "reasoning": "x", "evidence": []}
    )

    found = grade(result(scenario_id="false_alert", report=report), false_alert)

    assert verdicts(found)["cause"] == "pass" and verdicts(found)["routes"] == "pass"
    # Une route citée alors qu'il n'y a aucune panne
    report["affected_routes"] = ["/shipments/{order_id}"]
    assert verdicts(grade(result(report=report), false_alert))["routes"] == "fail"


def test_rules_version_is_frozen() -> None:
    assert ops_grading.rules_sha256() == FROZEN_RULES_SHA256, (
        "règles de notation de l'agent Ops modifiées : nouvelle version à déclarer"
    )


# --- Plusieurs enquêtes dans un scénario (J6.4) ---------------------------------------------

BOTH = SCENARIOS["db_and_payments"]


def test_each_investigation_is_graded_against_its_own_expectations() -> None:
    """Le rapport réel (paiements lents) est juste pour l'enquête « payments », faux pour
    l'enquête « orders » du même scénario."""
    payments = grade(result(scenario_id="db_and_payments", investigation="payments"), BOTH)
    orders = grade(result(scenario_id="db_and_payments", investigation="orders"), BOTH)

    assert payments.passed is True and payments.label == "db_and_payments · payments"
    assert orders.passed is False
    assert verdicts(orders)["cause"] == "fail" and orders.expected_cause == "database_errors"
    # Cause de l'autre panne avec une confiance élevée : faute grave
    high_confidence = {"confidence": "high"}
    high = grade(
        result(scenario_id="db_and_payments", investigation="orders", report=high_confidence),
        BOTH,
    )
    assert high.grave_fault


def test_a_route_of_the_other_fault_fails_routes_and_is_named() -> None:
    routes = ["/payments/{order_id}", "/orders/{order_id}"]

    found = grade(
        result(
            scenario_id="db_and_payments",
            investigation="payments",
            report={"affected_routes": routes},
        ),
        BOTH,
    )

    [routes_result] = [c for c in found.criteria if c.criterion.value == "routes"]
    assert routes_result.verdict.value == "fail"
    assert routes_result.reason == "en trop : /orders/{order_id} (autre panne)"
    # Une route d'aucune panne du scénario n'est pas dite « autre panne »
    unrelated = {"affected_routes": ["/payments/{order_id}", "/tickets"]}
    other = grade(result(report=unrelated), PAYMENTS)
    assert "en trop : /tickets" in other.criteria[2].reason
    assert "autre panne" not in other.criteria[2].reason


def test_file_from_before_several_investigations_is_read(tmp_path: Path) -> None:
    """Fichier de J6.3 : pas de champ `investigation`, l'enquête unique du scénario."""
    raw = json.loads(result().model_dump_json())
    del raw["investigation"]

    found = grade(ScenarioResult.model_validate(raw), PAYMENTS)

    assert found.passed is True and found.label == "payment_latency"


def test_report_lists_investigations(tmp_path: Path) -> None:
    rows = [
        result(scenario_id="db_and_payments", investigation="orders"),
        result(scenario_id="db_and_payments", investigation="payments"),
    ]
    path = eval_file(tmp_path / "ops.json", rows)

    assert ops_grade.main([str(path)], reports_dir=tmp_path / "docs") == 0

    text = (tmp_path / "docs" / "ops-2026-10-30T090000Z-test.md").read_text(encoding="utf-8")
    assert "**1 / 2 enquêtes réussies (50 %)**" in text
    assert "| `db_and_payments · orders` | `database_errors` | ✗" in text
    assert "| `db_and_payments · payments` | `slow_dependency` | ✓" in text


def test_command_refuses_an_unknown_investigation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = eval_file(tmp_path / "ops.json", [result(investigation="orders")])

    assert ops_grade.main([str(path)], reports_dir=tmp_path) == 1
    assert "enquête inconnue (payment_latency · orders)" in capsys.readouterr().err


# --- Rapport et commande ------------------------------------------------------------------


def eval_file(path: Path, scenarios: list[ScenarioResult], **changes: Any) -> Path:  # noqa: ANN401
    summary = OpsEvalSummary(
        name="test",
        series="2026-10-30T090000Z-test",
        model_requested="sonnet",
        models_used=["claude-sonnet-5-5"],
        prompt_sha256="a" * 64,
        truth_sha256=truth_sha256(INCIDENTS_PATH),
        scenarios_requested=[s.scenario_id for s in scenarios],
        max_budget_usd=5,
        total_cost_usd=0.07,
        status=RunStatus.COMPLETED,
        started_at=datetime(2026, 10, 30, 9, tzinfo=UTC),
    )
    report = OpsEvalReport(summary=summary.model_copy(update=changes), scenarios=scenarios)
    path.write_text(report.model_dump_json(), encoding="utf-8")
    return path


def test_report_and_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    no_alert = ScenarioResult(
        scenario_id="db_errors", status=ScenarioStatus.NO_ALERT, reason="alerte absente"
    )
    path = eval_file(tmp_path / "ops.json", [result(), no_alert])

    assert ops_grade.main([str(path)], reports_dir=tmp_path / "docs") == 0

    text = (tmp_path / "docs" / "ops-2026-10-30T090000Z-test.md").read_text(encoding="utf-8")
    assert "**1 / 1 enquêtes réussies (100 %)**" in text
    assert "fautes graves : **0**" in text
    assert "Exactes (chemin résolu) : 11" in text
    assert "introuvables : 0" in text and "fausse" not in text
    assert "`db_errors` p1 : no_alert (alerte absente)" in text
    assert "version de la notation" in text
    assert "$" not in text.replace(DOLLAR, "")
    out = capsys.readouterr().out
    assert "Score : 1 / 1 enquêtes réussies ; fautes graves : 0" in out


def test_command_refuses_another_ground_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = eval_file(tmp_path / "ops.json", [result()], truth_sha256="b" * 64)

    assert ops_grade.main([str(path)], reports_dir=tmp_path) == 1
    assert "vérité terrain des incidents changée" in capsys.readouterr().err


def test_command_refuses_passes_of_different_series(tmp_path: Path) -> None:
    first = eval_file(tmp_path / "p1.json", [result()])
    other = eval_file(tmp_path / "p2.json", [result()], series="autre", pass_index=2)

    assert ops_grade.main([str(first), str(other)], reports_dir=tmp_path) == 1


def test_report_calls_unfound_evidence_unfound_not_false(tmp_path: Path) -> None:
    """Même règle (le critère échoue), mais le rapport dit « introuvable », pas « fausse » :
    une preuve coupée ou mal indexée n'est pas forcément inventée."""
    report = copy.deepcopy(FIXTURE["output"])
    report["symptoms"][0]["evidence"][0]["value"] = 4999
    path = eval_file(tmp_path / "ops.json", [result(report=report)])

    assert ops_grade.main([str(path)], reports_dir=tmp_path / "docs") == 0

    text = (tmp_path / "docs" / "ops-2026-10-30T090000Z-test.md").read_text(encoding="utf-8")
    assert "introuvables : 1" in text
    assert "| `payment_latency` | p1 | `evidence` | 1 preuve(s) introuvable(s) |" in text
    assert "fausse" not in text
