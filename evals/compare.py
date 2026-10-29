"""Comparaison de deux configurations (J5.3) : deux séries renotées avec les mêmes règles.

    python -m evals.compare --base evals/results/<A>-p1.json <A>-p2.json \\
        --candidate evals/results/<B>-p1.json <B>-p2.json [--judge]

Chaque série est l'ensemble des passages d'une configuration (mêmes vérifications que
`evals.grade`). Les deux sont renotées ici, à partir des résultats bruts, avec la notation et
la vérité terrain en place : même version de la notation par construction (jamais d'ancien
rapport relu). Refus si les séries ne portent pas sur les mêmes tickets ou la même date
simulée, si elles ont la même consigne et le même modèle, ou si un passage s'est arrêté avant
la fin.

Rapport Markdown dans `docs/evals/compare-<A>-vs-<B>.md` (versionné). `--judge` : les deux
séries jugées dans la même exécution (même modèle, même grille), plafond commun, coût affiché.
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

from agentpilot.sdk_runner import QueryFn, sdk_query
from agentpilot.support_agent import AgentRunError
from evals import grading, judge
from evals.compare_report import decimal, render
from evals.comparison import JudgeComparison, compare_judge, compare_overall
from evals.grade import REPORTS_DIR, load, replies_to_judge, stem
from evals.ground_truth import TicketGroundTruth
from evals.report import Pass, Versions, percent, usd
from evals.results import EvalStatus
from evals.run import GROUND_TRUTH, Refused, budget, check_environment


def series(paths: Sequence[Path], truths: dict[int, TicketGroundTruth], role: str) -> list[Pass]:
    passes = []
    for path, report in load(paths):
        if report.summary.status is not EvalStatus.COMPLETED:
            raise Refused(
                f"{role} : {path.name} s'est arrêté avant la fin ({report.summary.status.value}) ;"
                " des tickets non lancés fausseraient la comparaison"
            )
        try:
            passes.append(Pass(path, report, grading.grade_report(report, truths)))
        except ValueError as exc:
            raise Refused(f"{path.name} : {exc}") from None
    return passes


def check_comparable(a: Sequence[Pass], b: Sequence[Pass]) -> None:
    first_a, first_b = a[0].report.summary, b[0].report.summary
    if sorted(first_a.tickets_requested) != sorted(first_b.tickets_requested):
        raise Refused("les deux séries ne portent pas sur les mêmes tickets")
    if first_a.simulation_now != first_b.simulation_now:
        raise Refused("les deux séries n'ont pas la même date simulée")
    if (
        first_a.code.prompt_sha256 == first_b.code.prompt_sha256
        and first_a.model_requested == first_b.model_requested
    ):
        raise Refused("même consigne et même modèle : rien à comparer")


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.compare",
        description="Compare deux séries d'évaluation notées avec les mêmes règles.",
    )
    parser.add_argument(
        "--base", nargs="+", type=Path, required=True, help="série de référence (A)"
    )
    parser.add_argument(
        "--candidate", nargs="+", type=Path, required=True, help="série candidate (B)"
    )
    parser.add_argument(
        "--out", type=Path, help="rapport (docs/evals/compare-<A>-vs-<B>.md par défaut)"
    )
    parser.add_argument("--judge", action="store_true", help="juge IA du ton et de la clarté")
    parser.add_argument(
        "--judge-model", default=judge.DEFAULT_MODEL, help=f"({judge.DEFAULT_MODEL} par défaut)"
    )
    parser.add_argument(
        "--judge-max-usd",
        type=budget,
        default=judge.DEFAULT_MAX_USD * 2,
        help=f"plafond du juge pour les deux séries ({judge.DEFAULT_MAX_USD * 2} $ par défaut)",
    )
    return parser


async def judge_both(
    a: Sequence[Pass],
    b: Sequence[Pass],
    *,
    model: str,
    max_usd: Decimal,
    query: QueryFn,
    environ: Mapping[str, str],
) -> JudgeComparison:
    """Les deux séries, l'une après l'autre, sous un plafond commun."""
    today = a[0].report.summary.simulation_now.date()
    first = await judge.judge(
        replies_to_judge(a), today=today, model=model, max_usd=max_usd, query=query, environ=environ
    )
    second = await judge.judge(
        replies_to_judge(b),
        today=today,
        model=model,
        max_usd=max(Decimal(0), max_usd - Decimal(str(first.cost_usd))),
        query=query,
        environ=environ,
    )
    return compare_judge(first, second)


def main(
    argv: Sequence[str] | None = None,
    *,
    ground_truth: Path = GROUND_TRUTH,
    reports_dir: Path = REPORTS_DIR,
    query: QueryFn = sdk_query,
    environ: Mapping[str, str] = os.environ,
    configure_logs: bool = True,
) -> int:
    """0 : rapport écrit ; 1 : refusé."""
    args = parser().parse_args(argv)
    try:
        if args.judge:
            check_environment(environ)
        truths = grading.load_truths(ground_truth)
        a = series(args.base, truths, "référence")
        b = series(args.candidate, truths, "candidate")
        check_comparable(a, b)
    except Refused as exc:
        print(f"Comparaison refusée : {exc}", file=sys.stderr)
        return 1
    if configure_logs:
        from agentpilot.app import log_dir
        from observability.logs import configure

        configure("agentpilot", log_dir(), stream=None)
    compared = None
    if args.judge:
        print(f"Juge IA : les deux séries, plafond {usd(float(args.judge_max_usd))}")
        try:
            compared = asyncio.run(
                judge_both(
                    a,
                    b,
                    model=args.judge_model,
                    max_usd=Decimal(args.judge_max_usd),
                    query=query,
                    environ=environ,
                )
            )
        except AgentRunError as exc:
            print(f"Comparaison refusée : {exc}", file=sys.stderr)
            return 1
    versions = Versions(
        rules_sha256=grading.rules_sha256(),
        truth_sha256=hashlib.sha256(ground_truth.read_bytes()).hexdigest(),
    )
    name = f"compare-{stem(a[0].report.summary)}-vs-{stem(b[0].report.summary)}.md"
    out = args.out or reports_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(a, b, judge=compared, compared_at=datetime.now(UTC), versions=versions)
    out.write_text(text, encoding="utf-8")
    overall = compare_overall([p.grades for p in a], [p.grades for p in b]).tickets
    word = "significatif" if overall.significant else "non significatif"
    print(
        f"A : {overall.a.passed} / {overall.a.total} ({percent(overall.a)}) ; "
        f"B : {overall.b.passed} / {overall.b.total} ({percent(overall.b)})"
    )
    print(
        f"Tickets : {overall.better} amélioré(s), {overall.worse} dégradé(s), {word} "
        f"(p = {decimal(overall.p_value, 3)})"
    )
    if compared is not None:
        print(f"Juge IA : {usd(compared.cost_usd)}")
    print(f"Version de la notation : {versions.rules_sha256[:12]}")
    print(f"Rapport : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
