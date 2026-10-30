"""Notation d'une évaluation de l'agent Ops (J6.3).

    python -m evals.ops_grade evals/results/ops-2026-10-30T090000Z-verif.json [--out rapport.md]

Plusieurs fichiers : les passages d'une même série (même nom, mêmes scénarios, même consigne,
même vérité terrain). Rapport dans `docs/evals/ops-<série>.md` (versionné). Refuse un fichier
noté contre une autre vérité terrain : les attentes ne seraient plus les mêmes.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from evals.incident_truth import INCIDENTS_PATH, load_scenarios, truth_sha256
from evals.ops_grading import grade, rules_sha256
from evals.ops_report import OpsPass, OpsVersions, render
from evals.ops_results import OpsEvalReport
from evals.run import REPO_ROOT

REPORTS_DIR = REPO_ROOT / "docs" / "evals"


class Refused(Exception):
    pass


def load(paths: Sequence[Path], truth_path: Path) -> list[OpsPass]:
    scenarios = {s.scenario_id: s for s in load_scenarios(truth_path)}
    truth = truth_sha256(truth_path)
    passes = []
    for path in paths:
        try:
            report = OpsEvalReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise Refused(f"{path.name} illisible : {exc}") from exc
        summary = report.summary
        if summary.truth_sha256 != truth:
            raise Refused(f"{path.name} : vérité terrain des incidents changée depuis l'évaluation")
        if passes:
            ref = passes[0].report.summary
            same = (ref.name, ref.scenarios_requested, ref.prompt_sha256, ref.series) == (
                summary.name,
                summary.scenarios_requested,
                summary.prompt_sha256,
                summary.series,
            )
            if not same:
                raise Refused(f"{path.name} n'est pas un passage de la même série")
        unknown = [r.scenario_id for r in report.scenarios if r.scenario_id not in scenarios]
        if unknown:
            raise Refused(f"{path.name} : scénario inconnu ({', '.join(unknown)})")
        grades = [grade(r, scenarios[r.scenario_id]) for r in report.scenarios]
        passes.append(OpsPass(path, report, grades))
    return sorted(passes, key=lambda p: p.report.summary.pass_index)


def main(
    argv: Sequence[str] | None = None,
    *,
    truth_path: Path = INCIDENTS_PATH,
    reports_dir: Path = REPORTS_DIR,
) -> int:
    """0 : rapport écrit ; 1 : refusé."""
    parser = argparse.ArgumentParser(prog="python -m evals.ops_grade")
    parser.add_argument("results", nargs="+", type=Path, help="passages d'une même série")
    parser.add_argument("--out", type=Path, help="rapport (docs/evals/ops-<série>.md sinon)")
    args = parser.parse_args(argv)
    try:
        passes = load(args.results, truth_path)
    except Refused as exc:
        print(f"Notation refusée : {exc}", file=sys.stderr)
        return 1
    out = args.out or reports_dir / f"ops-{passes[0].report.summary.series}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    versions = OpsVersions(rules_sha256(), truth_sha256(truth_path))
    out.write_text(render(passes, graded_at=datetime.now(UTC), versions=versions), encoding="utf-8")
    graded = [g for p in passes for g in p.grades if g.graded]
    passed = sum(1 for g in graded if g.passed)
    grave = sum(1 for g in graded if g.grave_fault)
    print(f"Score : {passed} / {len(graded)} scénarios réussis ; fautes graves : {grave}")
    for p in passes:
        for g in p.grades:
            state = "non noté" if g.passed is None else ("réussi" if g.passed else "échec")
            print(
                f"  p{p.report.summary.pass_index} {g.scenario_id:<26} {state} ({g.status.value})"
            )
    print(f"Version de la notation : {versions.rules_sha256[:12]}")
    print(f"Rapport : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
