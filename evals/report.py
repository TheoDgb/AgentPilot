"""Rapport Markdown d'une évaluation notée (J5.2), versionné dans `docs/evals/`.

Ne contient ni valeur interdite (seulement la clé trouvée) ni secret ; ne cite une réponse au
client (fictif) que pour un échec de fidélité.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean

from evals import grading
from evals.grading import Criterion, Score, TicketGrade, Verdict
from evals.judge import JudgeSummary
from evals.results import EvalReport

CRITERIA_LABELS = {
    Criterion.CATEGORY: "Bonne catégorie",
    Criterion.ACTION: "Bonne action (décision et ce qui s'est passé)",
    Criterion.APPROVAL: "Validation humaine correcte (décision et ce qui s'est passé)",
    Criterion.AMOUNT: "Bon montant",
    Criterion.FACTS: "Faits à dire au client, dans la réponse",
    Criterion.JUSTIFIED: "Décision justifiée par des faits réellement lus",
    Criterion.FORBIDDEN: "Aucune donnée interdite dans la réponse",
    Criterion.FAITHFUL: "Réponse fidèle à ce qui s'est passé",
}

LIMITS = """\
- Critères vérifiés par du code, sans IA : mêmes résultats → même note.
- Faits et annonces reconnus par une liste fermée de formulations françaises (`evals/facts.py`,
  `evals/fidelity.py`) : une formulation hors liste n'est pas reconnue. Un fait manquant peut
  donc être dit autrement ; la phrase d'un échec de fidélité est citée pour vérification.
- Faits partagés par catégorie, action et validation (`JUSTIFICATION` dans `evals/grading.py`) :
  à dire au client (dans la réponse), ou de justification (dans la réponse, le motif
  d'escalade ou `facts_used`, avec une liste fermée d'équivalents comme `days_since_delivery`),
  ou non notés (`UNGRADED` : ni utiles au client ni fondement de la décision).
- `facts_used` vérifié dans les réponses d'outils gardées au journal ; une réponse tronquée
  (plus de 4 000 caractères) ne compte pas contre l'agent. Fichier de résultats d'avant J5.2,
  sans lectures enregistrées : faits cités admis sans vérification (signalé dans l'en-tête).
- Non noté (la vérité terrain ne le prévoit pas) : résoudre ou non le ticket, le nombre de
  réponses, le texte du motif d'escalade (hors faits), une escalade en plus d'un remboursement
  ou d'un renvoi attendu.
- Exécution en échec : critères de la décision en échec ; faits, données interdites et fidélité
  jugés sur ce qui a réellement été envoyé.
- Tickets non lancés ou interrompus (budget, Ctrl+C, abandon) : hors des scores.
"""


@dataclass(frozen=True)
class Pass:
    path: Path
    report: EvalReport
    grades: list[TicketGrade]


def percent(score: Score) -> str:
    return "—" if score.rate is None else f"{score.rate * 100:.0f} %"


def cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def usd(amount: float | None) -> str:
    return "inconnu" if amount is None else f"{amount:.4f} $".replace(".", ",")


def label(index: int, passes: int) -> str:
    return f"p{index}" if passes > 1 else ""


@dataclass(frozen=True)
class Versions:
    """Empreintes des règles de notation et de la vérité terrain utilisées."""

    rules_sha256: str
    truth_sha256: str


def render(
    passes: Sequence[Pass],
    *,
    judge: JudgeSummary | None = None,
    graded_at: datetime,
    versions: Versions,
) -> str:
    first = passes[0].report.summary
    all_grades = [g for p in passes for g in p.grades]
    count = len(passes)
    lines = [f"# Évaluation « {first.name} »", ""]
    lines += _header(passes, graded_at, versions)
    lines += _overall(passes, all_grades)
    lines += _criteria(all_grades)
    lines += _categories(all_grades)
    lines += _costs(passes, judge)
    lines += _failures(passes)
    lines += _not_graded(passes)
    if count > 1:
        lines += _variability(passes)
    if judge is not None:
        lines += _judge(judge)
    lines += ["## Limites de la notation", "", LIMITS]
    return "\n".join(lines)


def _header(passes: Sequence[Pass], graded_at: datetime, versions: Versions) -> list[str]:
    summary = passes[0].report.summary
    code = summary.code
    commit = (code.commit or "inconnu")[:12]
    dirty = " (modifications non commitées)" if code.dirty else ""
    models = sorted({m for p in passes for m in p.report.summary.models_used})
    states = ", ".join(
        f"{label(i, len(passes)) or 'passage'} {p.report.summary.status.value}"
        for i, p in enumerate(passes, start=1)
    )
    files = ", ".join(f"`{p.path.name}`" for p in passes)
    unread = [p.path.name for p in passes if any(t.reads is None for t in p.report.tickets)]
    return [
        f"- Configuration : `{summary.name}`, modèle demandé `{summary.model_requested}`, "
        f"modèles utilisés {', '.join(f'`{m}`' for m in models) or 'inconnus'}",
        f"- Code : commit `{commit}`{dirty}, empreinte de la consigne `{code.prompt_sha256[:12]}`",
        f"- Date simulée : {summary.simulation_now:%Y-%m-%d %H:%M} UTC ; lancée le "
        f"{summary.started_at:%Y-%m-%d %H:%M} UTC",
        f"- Passages : {len(passes)} ({states})",
        f"- Fichiers de résultats : {files}",
        f"- Noté le {graded_at:%Y-%m-%d %H:%M} UTC par `python -m evals.grade`, version de la "
        f"notation `{versions.rules_sha256[:12]}`, vérité terrain `{versions.truth_sha256[:12]}`",
        *(
            [
                f"- Lectures d'outils non enregistrées ({', '.join(unread)}, d'avant J5.2) : "
                "`facts_used` admis sans vérification"
            ]
            if unread
            else []
        ),
        "",
    ]


def _overall(passes: Sequence[Pass], grades: list[TicketGrade]) -> list[str]:
    score = grading.overall(grades)
    not_graded = sum(g.passed is None for g in grades)
    lines = [
        "## Score global",
        "",
        f"**{score.passed} / {score.total} tickets réussis ({percent(score)})** sur tous leurs "
        "critères applicables"
        + (", tous passages confondus" if len(passes) > 1 else "")
        + f". Non notés : {not_graded}.",
        "",
    ]
    if len(passes) > 1:
        rates = []
        lines += ["| Passage | Réussis | Notés | Score |", "|---|---|---|---|"]
        for index, p in enumerate(passes, start=1):
            s = grading.overall(p.grades)
            if s.rate is not None:
                rates.append(s.rate)
            lines.append(f"| p{index} | {s.passed} | {s.total} | {percent(s)} |")
        stable = grading.stable_passes([p.grades for p in passes])
        lines += [""]
        if rates:
            lines.append(
                f"Moyenne {mean(rates) * 100:.0f} %, minimum {min(rates) * 100:.0f} %, "
                f"maximum {max(rates) * 100:.0f} %."
            )
        lines += [
            f"pass^{len(passes)} (réussis à chaque passage) : {stable.passed} / {stable.total} "
            f"({percent(stable)}).",
            "",
        ]
    return lines


def _criteria(grades: list[TicketGrade]) -> list[str]:
    scores = grading.by_criterion(grades)
    lines = [
        "## Score par critère",
        "",
        "| Critère | Réussis | Cas | Taux |",
        "|---|---|---|---|",
    ]
    for criterion, score in scores.items():
        lines.append(
            f"| {CRITERIA_LABELS[criterion]} (`{criterion.value}`) | {score.passed} | "
            f"{score.total} | {percent(score)} |"
        )
    noted = grading.graded(grades)
    found = sum(g.facts_found for g in noted)
    total = sum(g.facts_total for g in noted)
    facts = Score(passed=found, total=total)
    lines += ["", f"Faits à dire trouvés un par un : {found} / {total} ({percent(facts)}).", ""]
    return lines


def _categories(grades: list[TicketGrade]) -> list[str]:
    lines = [
        "## Score par catégorie de ticket",
        "",
        "| Catégorie | Réussis | Tickets | Taux | Critères en échec |",
        "|---|---|---|---|---|",
    ]
    for category, score in grading.by_category(grades).items():
        failed: dict[str, int] = {}
        for grade in grading.graded(grades):
            if grade.category != category:
                continue
            for result in grade.criteria:
                if result.verdict is Verdict.FAIL:
                    failed[result.criterion.value] = failed.get(result.criterion.value, 0) + 1
        detail = ", ".join(f"`{c}` ×{n}" for c, n in failed.items()) or "—"
        lines.append(
            f"| {category} | {score.passed} | {score.total} | {percent(score)} | {detail} |"
        )
    return [*lines, ""]


def _costs(passes: Sequence[Pass], judge: JudgeSummary | None) -> list[str]:
    grades = [(i, g) for i, p in enumerate(passes, start=1) for g in p.grades]
    costs = [(i, g) for i, g in grades if g.cost_usd is not None]
    durations = [(i, g) for i, g in grades if g.duration_ms is not None]
    total = sum(p.report.summary.total_cost_usd for p in passes)
    count = len(passes)
    lines = [
        "## Coût et durée",
        "",
        f"- Coût total de l'évaluation : {usd(total)} (vérifications d'isolement comprises ; "
        f"plafond {usd(passes[0].report.summary.max_budget_usd)})",
    ]
    if costs:
        i, dearest = max(costs, key=lambda x: x[1].cost_usd or 0)
        average = mean(g.cost_usd or 0 for _, g in costs)
        where = f" {label(i, count)}" if count > 1 else ""
        lines.append(
            f"- Par ticket : moyenne {usd(average)}, le plus cher ticket {dearest.ticket_id}"
            f"{where} ({usd(dearest.cost_usd)})"
        )
    if durations:
        i, slowest = max(durations, key=lambda x: x[1].duration_ms or 0)
        average_s = mean((g.duration_ms or 0) / 1000 for _, g in durations)
        where = f" {label(i, count)}" if count > 1 else ""
        lines.append(
            f"- Durée par ticket : moyenne {average_s:.1f} s, le plus lent ticket "
            f"{slowest.ticket_id}{where} ({(slowest.duration_ms or 0) / 1000:.1f} s)".replace(
                ".", ","
            )
        )
    if judge is not None:
        lines.append(f"- Juge IA : {usd(judge.cost_usd)} (plafond {usd(judge.max_usd)})")
    return [*lines, ""]


def _failures(passes: Sequence[Pass]) -> list[str]:
    count = len(passes)
    rows = []
    for index, p in enumerate(passes, start=1):
        for grade in p.grades:
            for result in grade.criteria:
                if result.verdict is Verdict.FAIL:
                    rows.append(
                        ([f"p{index}"] if count > 1 else [])
                        + [
                            str(grade.ticket_id),
                            grade.category,
                            f"`{result.criterion.value}`",
                            cell(result.reason or ""),
                        ]
                    )
    lines = ["## Tickets en échec", ""]
    if not rows:
        return [*lines, "Aucun.", ""]
    head = (["Passage"] if count > 1 else []) + ["Ticket", "Catégorie", "Critère", "Raison"]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return [*lines, ""]


def _not_graded(passes: Sequence[Pass]) -> list[str]:
    count = len(passes)
    items = [
        f"{label(i, count) + ' ' if count > 1 else ''}ticket {g.ticket_id} ({g.status.value})"
        for i, p in enumerate(passes, start=1)
        for g in p.grades
        if g.passed is None
    ]
    if not items:
        return []
    return ["## Tickets non notés", "", ", ".join(items), ""]


def _variability(passes: Sequence[Pass]) -> list[str]:
    found = grading.unstable([p.grades for p in passes])
    lines = ["## Variabilité", ""]
    if not found:
        return [
            *lines,
            "Aucun ticket instable : mêmes verdicts et mêmes actions à chaque passage.",
            "",
        ]
    lines += [
        f"{len(found)} ticket(s) instable(s) : un verdict ou l'action réellement faite change "
        "d'un passage à l'autre.",
        "",
        "| Ticket | Catégorie | Ce qui change | Passages |",
        "|---|---|---|---|",
    ]
    for item in found:
        outcomes = " ; ".join(
            f"p{o.pass_index} {'✓' if o.passed else '✗'}"
            + (f" ({', '.join(c.value for c in o.failed)})" if o.failed else "")
            + f" {o.actions}"
            for o in item.outcomes
        )
        changes = ", ".join(f"`{c}`" for c in item.changes)
        lines.append(f"| {item.ticket_id} | {item.category} | {changes} | {cell(outcomes)} |")
    return [*lines, ""]


def _judge(judge: JudgeSummary) -> list[str]:
    scored = [i for i in judge.items if i.score is not None]
    lines = [
        "## Juge IA : ton et clarté (facultatif)",
        "",
        "> Noté par un modèle, non déterministe : jamais compté dans le score global.",
        "",
        f"- Modèle `{judge.model}`, grille `evals/judge_rubric.md` "
        f"(empreinte `{judge.rubric_sha256[:12]}`), coût {usd(judge.cost_usd)}",
        f"- Réponses jugées : {len(scored)} ; en erreur : {len(judge.items) - len(scored)} ; "
        f"non jugées (plafond atteint) : {judge.skipped}",
    ]
    if scored:
        tone = mean(i.score.tone for i in scored if i.score)
        clarity = mean(i.score.clarity for i in scored if i.score)
        lines.append(f"- Moyennes sur 5 : ton {tone:.1f}, clarté {clarity:.1f}".replace(".", ","))
        low = [i for i in scored if i.score and min(i.score.tone, i.score.clarity) <= 3]
        if low:
            lines += [
                "",
                "| Ticket | Passage | Ton | Clarté | Justification |",
                "|---|---|---|---|---|",
            ]
            for item in low:
                assert item.score is not None
                lines.append(
                    f"| {item.ticket_id} | p{item.pass_index} | {item.score.tone} | "
                    f"{item.score.clarity} | {cell(item.score.justification)} |"
                )
    return [*lines, ""]
