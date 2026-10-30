"""Rapport Markdown d'une évaluation de l'agent Ops (`evals.ops_grade`), versionné dans
`docs/evals/ops-<série>.md`. Chaque « $ » est écrit `<span>$</span>` (`evals.report.money`)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evals.ops_evidence import EvidenceVerdict
from evals.ops_grading import Criterion, CriterionResult, ScenarioGrade, Verdict
from evals.ops_results import OpsEvalReport
from evals.report import cell, money


@dataclass
class OpsPass:
    path: Path
    report: OpsEvalReport
    grades: list[ScenarioGrade]


@dataclass(frozen=True)
class OpsVersions:
    rules_sha256: str
    truth_sha256: str


def _rate(passed: int, total: int) -> str:
    return "—" if total == 0 else f"{passed / total * 100:.0f} %"


def _models(passes: Sequence[OpsPass]) -> str:
    models = sorted({m for p in passes for m in p.report.summary.models_used})
    return ", ".join(f"`{m}`" for m in models) or "inconnus"


def _mark(grade: ScenarioGrade) -> str:
    if grade.passed is None:
        return f"— ({grade.status.value})"
    cause = grade.found_cause or "aucun rapport"
    return f"{'✓' if grade.passed else '✗'} {cause} ({grade.confidence or '—'})"


def render(passes: Sequence[OpsPass], *, graded_at: datetime, versions: OpsVersions) -> str:
    first = passes[0].report.summary
    grades = [g for p in passes for g in p.grades]
    graded = [g for g in grades if g.graded]
    passed = sum(1 for g in graded if g.passed)
    lines = [
        f"# Évaluation de l'agent Ops : {first.series}",
        "",
        f"- Configuration : `{first.name}`, modèle demandé `{first.model_requested}`, "
        f"modèles utilisés {_models(passes)}",
        f"- Code : commit `{(first.commit or 'inconnu')[:12]}`"
        f"{' (modifications non commitées)' if first.dirty else ''}, consigne Ops "
        f"`{first.prompt_sha256[:12]}`",
        f"- Passages : {len(passes)} ("
        + ", ".join(
            f"p{p.report.summary.pass_index} {p.report.summary.status.value}" for p in passes
        )
        + ")",
        f"- Fichiers de résultats : {', '.join(f'`{p.path.name}`' for p in passes)}",
        f"- Noté le {graded_at:%Y-%m-%d %H:%M} UTC par `python -m evals.ops_grade`, version de la "
        f"notation `{versions.rules_sha256[:12]}`, vérité terrain `{versions.truth_sha256[:12]}`",
        "",
        "## Score",
        "",
        f"**{passed} / {len(graded)} scénarios réussis ({_rate(passed, len(graded))})** ; "
        f"fautes graves : **{sum(1 for g in graded if g.grave_fault)}**.",
        "",
        "Un scénario réussit si son rapport est valide, sa cause et ses routes justes, toutes ses "
        "preuves retrouvées, au moins une hypothèse écartée (jamais la bonne) et aucune confiance "
        "élevée sur une mauvaise cause.",
        "",
        "## Par critère",
        "",
        "| Critère | Réussis | Notés | Taux |",
        "|---|---|---|---|",
    ]
    for criterion in Criterion:
        verdicts = [g.verdict(criterion) for g in graded]
        ok = sum(v is Verdict.PASS for v in verdicts)
        total = sum(v is not Verdict.NOT_APPLICABLE for v in verdicts)
        lines.append(f"| `{criterion.value}` | {ok} | {total} | {_rate(ok, total)} |")
    lines += ["", "## Par scénario", ""]
    header = " | ".join(f"p{p.report.summary.pass_index}" for p in passes)
    lines += [
        f"| Scénario | Cause attendue | {header} | pass^k |",
        "|---|---|" + "---|" * len(passes) + "---|",
    ]
    for scenario_id in first.scenarios_requested:
        row = [g for p in passes for g in p.grades if g.scenario_id == scenario_id]
        if not row:
            continue
        scored = [g for g in row if g.graded]
        all_ok = "—" if not scored else ("oui" if all(g.passed for g in scored) else "non")
        lines.append(
            f"| `{scenario_id}` | `{row[0].expected_cause}` | "
            + " | ".join(cell(_mark(g)) for g in row)
            + f" | {all_ok} |"
        )
    lines += ["", *_failures(passes), *_evidence(passes), *_not_graded(passes), *_costs(passes)]
    lines += [
        "## Limites",
        "",
        "- Les preuves sont recoupées avec les réponses d'outils gardées au journal : une valeur "
        "banale (0, 1, vrai, faux, vide) dont le chemin ne se résout pas est invérifiable, ni "
        "validée ni comptée en faute.",
        "- Six scénarios, une panne ou un enchaînement chacun : une réussite ici ne dit rien des "
        "pannes réelles qui ne leur ressemblent pas.",
        "- Les durées des pannes et le moment de l'enquête sont fixés par la vérité terrain ; une "
        "enquête plus tôt ou plus tard peut voir d'autres symptômes.",
        "",
    ]
    return "\n".join(lines)


def _failure_reason(grade: ScenarioGrade, criterion: CriterionResult) -> str:
    """Motif affiché ; pour les preuves, le nombre d'introuvables (même règle de notation)."""
    if criterion.criterion is Criterion.EVIDENCE:
        missing = grade.evidence_counts()[EvidenceVerdict.FABRICATED]
        return f"{missing} preuve(s) introuvable(s)"
    return criterion.reason


def _failures(passes: Sequence[OpsPass]) -> list[str]:
    rows = []
    for p in passes:
        for g in p.grades:
            for c in g.criteria:
                if c.verdict is Verdict.FAIL:
                    rows.append(
                        f"| `{g.scenario_id}` | p{p.report.summary.pass_index} | "
                        f"`{c.criterion.value}` | {cell(_failure_reason(g, c))} |"
                    )
    grave = [
        f"- `{g.scenario_id}` p{p.report.summary.pass_index} : `{g.found_cause}` avec une "
        f"confiance élevée (attendu `{g.expected_cause}`)"
        for p in passes
        for g in p.grades
        if g.grave_fault
    ]
    low = [
        f"- `{g.scenario_id}` p{p.report.summary.pass_index} : bonne cause, confiance faible"
        for p in passes
        for g in p.grades
        if g.low_confidence
    ]
    lines = ["## Échecs", ""]
    lines += (
        ["| Scénario | Passage | Critère | Raison |", "|---|---|---|---|", *rows]
        if rows
        else ["Aucun critère en échec."]
    )
    lines += ["", "## Fautes graves", "", *(grave or ["Aucune."]), ""]
    if low:
        lines += ["Signalé, sans échec :", "", *low, ""]
    return lines


def _evidence(passes: Sequence[OpsPass]) -> list[str]:
    counts = {v: 0 for v in EvidenceVerdict}
    wrong = []
    for p in passes:
        for g in p.grades:
            for e in g.evidence:
                counts[e.verdict] += 1
                if e.verdict is EvidenceVerdict.FABRICATED:
                    wrong.append(
                        f"| `{g.scenario_id}` | p{p.report.summary.pass_index} | `{e.tool}` | "
                        f"{cell(e.field)} | {cell(repr(e.value))} | {cell(e.reason)} |"
                    )
    lines = [
        "## Preuves",
        "",
        f"Exactes (chemin résolu) : {counts[EvidenceVerdict.EXACT]} ; retrouvées dans la réponse : "
        f"{counts[EvidenceVerdict.FOUND]} ; invérifiables (valeur banale) : "
        f"{counts[EvidenceVerdict.UNVERIFIABLE]} ; introuvables : "
        f"{counts[EvidenceVerdict.FABRICATED]}.",
        "",
        "Une preuve introuvable n'est pas forcément inventée : sa valeur ne se retrouve pas telle "
        "quelle à son chemin dans la réponse de l'outil (valeur différente, coupée, deux champs "
        "réunis, mauvais index). Elle ne prouve donc rien, et le critère échoue.",
        "",
    ]
    if wrong:
        lines += [
            "| Scénario | Passage | Outil | Champ | Valeur citée | Raison |",
            "|---|---|---|---|---|---|",
            *wrong,
            "",
        ]
    return lines


def _not_graded(passes: Sequence[OpsPass]) -> list[str]:
    items = [
        f"- `{g.scenario_id}` p{p.report.summary.pass_index} : {g.status.value}"
        + (f" ({cell(g.reason)})" if g.reason else "")
        for p in passes
        for g in p.grades
        if not g.graded
    ]
    return ["## Scénarios non notés", "", *items, ""] if items else []


def _costs(passes: Sequence[OpsPass]) -> list[str]:
    total = sum(p.report.summary.total_cost_usd for p in passes)
    lines = ["## Coût et durée", "", f"- Coût total : {money(total)}", ""]
    rows = []
    for p in passes:
        for g in p.grades:
            if g.cost_usd is not None:
                seconds = f"{(g.duration_ms or 0) / 1000:.0f} s".replace(".", ",")
                rows.append(
                    f"| `{g.scenario_id}` | p{p.report.summary.pass_index} | "
                    f"{money(g.cost_usd)} | {seconds} |"
                )
    if rows:
        lines += [
            "| Scénario | Passage | Coût | Durée de l'enquête |",
            "|---|---|---|---|",
            *rows,
            "",
        ]
    return lines
