"""Notation et rapport d'une évaluation (J5.2) : résultats de `evals.run` contre la vérité terrain.

    python -m evals.grade evals/results/2026-10-28T164025Z-essai.json
    python -m evals.grade evals/results/*-variabilite-p*.json --judge

Plusieurs fichiers : les passages d'une même configuration (même nom, mêmes tickets, même
consigne), pour la variabilité. Critères vérifiés par du code (`evals/grading.py`) ; juge IA
facultatif pour le ton et la clarté (`--judge`, `evals/judge.py`), hors score, coût affiché.

Seuils de réussite (`--thresholds evals/thresholds.toml`, J5.4) : vérifiés après la notation,
section dans le rapport ; code de sortie 3 et seuil nommé s'il n'est pas atteint.

Rapport Markdown dans `docs/evals/<date>-<nom>.md` (versionné ; même fichier si l'on renote les
mêmes résultats). Écran : score et chemin du rapport ; logs JSON dans `logs/` seulement.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from agentpilot.sdk_runner import QueryFn, sdk_query
from agentpilot.support_agent import AgentRunError
from evals import grading, judge, thresholds
from evals.report import Gate, Pass, Versions, percent, render, usd
from evals.results import EvalReport, EvalSummary, TicketStatus, infra_failure
from evals.run import GROUND_TRUTH, REPO_ROOT, Refused, budget, check_environment

REPORTS_DIR = REPO_ROOT / "docs" / "evals"


def load(paths: Sequence[Path]) -> list[tuple[Path, EvalReport]]:
    loaded = []
    for path in paths:
        try:
            loaded.append((path, EvalReport.model_validate_json(path.read_text(encoding="utf-8"))))
        except (OSError, ValidationError) as exc:
            raise Refused(
                f"fichier de résultats illisible : {path} ({type(exc).__name__})"
            ) from None
    for path, report in loaded:
        # Fichier d'avant la distinction : une erreur d'infrastructure y passe pour une faute
        broken = [
            t.ticket_id
            for t in report.tickets
            if t.status is TicketStatus.FAILED and infra_failure(t)
        ]
        if broken:
            raise Refused(
                f"{path.name} : {len(broken)} ticket(s) ({shown(broken)}) en échec à cause de "
                "l'infrastructure (API Anthropic, ShopSim, configuration ou CLI), enregistrés "
                "comme des fautes de l'agent : relancer l'évaluation"
            )
    first = loaded[0][1].summary
    for path, report in loaded[1:]:
        summary = report.summary
        same = (
            summary.name == first.name
            and summary.tickets_requested == first.tickets_requested
            and summary.code.prompt_sha256 == first.code.prompt_sha256
            and summary.model_requested == first.model_requested
        )
        if not same:
            raise Refused(
                f"{path.name} n'est pas un passage de la même configuration que "
                f"{loaded[0][0].name} (nom, tickets, modèle ou consigne différents)"
            )
    # Ordre des passages : rang dans la série, puis date
    return sorted(loaded, key=lambda x: (x[1].summary.pass_index, x[1].summary.started_at))


def shown(ids: Sequence[int], limit: int = 10) -> str:
    more = f" (+{len(ids) - limit})" if len(ids) > limit else ""
    return ", ".join(map(str, ids[:limit])) + more


def stem(summary: EvalSummary) -> str:
    """Même nom que les résultats : identifiant de la série, sinon date et nom."""
    return summary.series or f"{summary.started_at:%Y-%m-%dT%H%M%SZ}-{summary.name}"


def replies_to_judge(passes: Sequence[Pass]) -> list[judge.Reply]:
    replies = []
    for index, p in enumerate(passes, start=1):
        by_id = {t.ticket_id: t for t in p.report.tickets}
        for grade in grading.graded(p.grades):
            sent = by_id[grade.ticket_id].replies
            if sent:
                replies.append(judge.Reply(grade.ticket_id, index, "\n\n---\n\n".join(sent)))
    return replies


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.grade",
        description="Note une évaluation contre la vérité terrain et écrit un rapport Markdown.",
    )
    parser.add_argument("results", nargs="+", type=Path, help="fichier(s) de evals/results/")
    parser.add_argument("--out", type=Path, help="rapport (docs/evals/<date>-<nom>.md par défaut)")
    parser.add_argument(
        "--thresholds",
        type=Path,
        help="seuils de réussite (evals/thresholds.toml) : code 3 s'ils ne sont pas atteints",
    )
    parser.add_argument("--judge", action="store_true", help="juge IA du ton et de la clarté")
    parser.add_argument(
        "--judge-model", default=judge.DEFAULT_MODEL, help=f"({judge.DEFAULT_MODEL} par défaut)"
    )
    parser.add_argument(
        "--judge-max-usd",
        type=budget,
        default=judge.DEFAULT_MAX_USD,
        help=f"plafond du juge ({judge.DEFAULT_MAX_USD} $ par défaut)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    ground_truth: Path = GROUND_TRUTH,
    reports_dir: Path = REPORTS_DIR,
    query: QueryFn = sdk_query,
    environ: Mapping[str, str] = os.environ,
    configure_logs: bool = True,
) -> int:
    """0 : rapport écrit ; 1 : refusé ; 3 : rapport écrit, seuil de réussite non atteint."""
    args = parser().parse_args(argv)
    try:
        if args.judge:
            check_environment(environ)
        truths = grading.load_truths(ground_truth)
        limits = thresholds.load(args.thresholds) if args.thresholds else None
        passes = []
        for path, report in load(args.results):
            try:
                passes.append(Pass(path, report, grading.grade_report(report, truths)))
            except ValueError as exc:
                raise Refused(f"{path.name} : {exc}") from None
    except (Refused, thresholds.InvalidThresholds, OSError) as exc:
        print(f"Notation refusée : {exc}", file=sys.stderr)
        return 1
    if configure_logs:
        from agentpilot.app import log_dir
        from observability.logs import configure

        configure("agentpilot", log_dir(), stream=None)
    summary = None
    if args.judge:
        replies = replies_to_judge(passes)
        print(f"Juge IA : {len(replies)} réponse(s), plafond {usd(float(args.judge_max_usd))}")
        try:
            summary = asyncio.run(
                judge.judge(
                    replies,
                    today=passes[0].report.summary.simulation_now.date(),
                    model=args.judge_model,
                    max_usd=Decimal(args.judge_max_usd),
                    query=query,
                    environ=environ,
                )
            )
        except AgentRunError as exc:
            print(f"Notation refusée : {exc}", file=sys.stderr)
            return 1
    out = args.out or reports_dir / f"{stem(passes[0].report.summary)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    versions = Versions(
        rules_sha256=grading.rules_sha256(),
        truth_sha256=hashlib.sha256(ground_truth.read_bytes()).hexdigest(),
    )
    gate = None
    if limits is not None:
        outcomes, failures = thresholds.check(
            [p.report for p in passes], [p.grades for p in passes], limits
        )
        gate = Gate(str(args.thresholds), limits.sha256, outcomes, failures)
    text = render(passes, judge=summary, graded_at=datetime.now(UTC), versions=versions, gate=gate)
    out.write_text(text, encoding="utf-8")
    score = grading.overall([g for p in passes for g in p.grades])
    print(f"Score : {score.passed} / {score.total} tickets réussis ({percent(score)})")
    for criterion, s in grading.by_criterion([g for p in passes for g in p.grades]).items():
        print(f"  {criterion.value:<10} {s.passed} / {s.total} ({percent(s)})")
    if summary is not None:
        print(f"Juge IA : {usd(summary.cost_usd)} ({len(summary.items)} réponse(s) jugée(s))")
    print(f"Version de la notation : {versions.rules_sha256[:12]}")
    print(f"Rapport : {out}")
    if gate is not None:
        for failure in gate.failures:
            print(failure, file=sys.stderr)
        if gate.failures:
            return 3
        print("Seuils de réussite atteints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
