"""Comparaison de deux séries de l'agent Ops (`evals.ops_compare`, J6.4) : refus, régressions,
écarts de coût et de durée, appels par agent, verdict de la règle de décision."""

import copy
from pathlib import Path
from typing import Any

import pytest

from evals import ops_compare
from evals.ops_results import RunStatus, ScenarioResult
from evals.report import DOLLAR
from evals.tests.test_ops_grading import FIXTURE, eval_file, result


def write(
    tmp_path: Path,
    name: str,
    rows: list[ScenarioResult],
    *,
    architecture: str = "solo",
    prompt: str = "a" * 64,
    **changes: Any,  # noqa: ANN401
) -> Path:
    return eval_file(
        tmp_path / f"{name}.json",
        rows,
        name=name,
        series=f"2026-10-30T090000Z-{name}",
        architecture=architecture,
        prompt_sha256=prompt,
        **changes,
    )


def subagent_calls() -> list[dict[str, Any]]:
    """Les appels réels de la fixture, faits par les subagents, avec leurs lancements."""
    calls = copy.deepcopy(FIXTURE["tool_calls"])
    for call in calls:
        short = call["tool"].rsplit("__", 1)[-1]
        call["agent"] = (
            "metrics-investigator"
            if short == "query_metrics"
            else "logs-investigator"
            if short in ("search_logs", "summarize_logs")
            else None
        )
    launches = [
        {"tool": "Agent", "input": {"subagent_type": t}, "outcome": "succeeded", "agent": None}
        for t in ("logs-investigator", "metrics-investigator")
    ]
    return launches + calls


def run(tmp_path: Path, base: Path, candidate: Path) -> tuple[int, str]:
    out = tmp_path / "compare.md"
    code = ops_compare.main(
        ["--base", str(base), "--candidate", str(candidate), "--out", str(out)],
        reports_dir=tmp_path,
    )
    return code, out.read_text(encoding="utf-8") if out.exists() else ""


def candidate(tmp_path: Path, **changes: Any) -> Path:  # noqa: ANN401
    row = result(tool_calls=subagent_calls(), **changes)
    return write(tmp_path, "subagents", [row], architecture="subagents", prompt="b" * 64)


def base(tmp_path: Path) -> Path:
    return write(tmp_path, "reference", [result()])


def test_cheaper_without_regression_is_kept(tmp_path: Path) -> None:
    code, text = run(tmp_path, base(tmp_path), candidate(tmp_path, cost_usd=0.05))

    assert code == 0
    assert "**Candidate retenue** : sans régression, moins chère." in text
    assert "| Coût | 0,0682" in text and "-27 %" in text
    assert "$" not in text.replace(DOLLAR, "")


def test_no_gain_is_not_kept(tmp_path: Path) -> None:
    slower = candidate(tmp_path, cost_usd=0.09, duration_ms=30000)

    code, text = run(tmp_path, base(tmp_path), slower)

    assert code == 0
    assert (
        "**Candidate pas retenue** : sans régression, mais ni moins chère ni plus rapide." in text
    )


def test_a_failed_investigation_is_a_regression(tmp_path: Path) -> None:
    wrong = candidate(tmp_path, cost_usd=0.01, report={"probable_cause": "resource_saturation"})

    code, text = run(tmp_path, base(tmp_path), wrong)

    assert code == 0
    assert "**Candidate pas retenue** : 1 régression(s)." in text
    assert "| `payment_latency` | 100 % (slow_dependency) | 0 % (resource_saturation) |" in text
    assert "taux de réussite en baisse" in text and "en échec : cause" in text


def test_an_unfound_evidence_is_a_regression(tmp_path: Path) -> None:
    """Une preuve déformée en passant d'un agent à l'autre : le risque principal."""
    report = copy.deepcopy(FIXTURE["output"])
    report["symptoms"][0]["evidence"][0]["value"] = 4999

    code, text = run(tmp_path, base(tmp_path), candidate(tmp_path, cost_usd=0.01, report=report))

    assert code == 0
    assert "pas retenue" in text and "preuves introuvables : 0 → 1" in text


def test_calls_are_counted_by_agent_and_launches_are_not_reads(tmp_path: Path) -> None:
    code, text = run(tmp_path, base(tmp_path), candidate(tmp_path))

    assert code == 0
    reads = len(FIXTURE["tool_calls"])
    assert f"| Appels aux outils de lecture | {reads} | {reads} | +0 % |" in text
    assert "logs-investigator" in text and "metrics-investigator" in text


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        # Même architecture, même consigne : rien à comparer
        ({"architecture": "solo", "prompt": "a" * 64}, "rien à comparer"),
        # Passage arrêté avant la fin
        ({"status": RunStatus.INTERRUPTED}, "arrêté avant la fin"),
    ],
)
def test_refusals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
    message: str,
) -> None:
    fields = {"architecture": "subagents", "prompt": "b" * 64} | changes
    other = write(tmp_path, "other", [result()], **fields)

    code, _ = run(tmp_path, base(tmp_path), other)

    assert code == 1 and message in capsys.readouterr().err


def test_different_investigations_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = write(
        tmp_path,
        "other",
        [result(scenario_id="db_and_payments", investigation="payments")],
        architecture="subagents",
    )

    code, _ = run(tmp_path, base(tmp_path), other)

    assert code == 1 and "mêmes enquêtes" in capsys.readouterr().err
