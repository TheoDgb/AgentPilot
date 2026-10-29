"""Rapport Markdown d'une comparaison de deux configurations (J5.3), versionné dans `docs/evals/`.

Comme le rapport d'une évaluation : ni valeur interdite, ni secret, ni texte de client (les
raisons d'échec sont dans les rapports de chaque série).
"""

from collections.abc import Sequence
from datetime import datetime
from statistics import mean

from evals.comparison import (
    ALPHA,
    JUDGE_SHOWN_DELTA,
    Delta,
    JudgeComparison,
    JudgeDelta,
    compare_categories,
    compare_criteria,
    compare_overall,
    ticket_changes,
)
from evals.grading import PassOutcome, TicketGrade
from evals.report import CRITERIA_LABELS, Pass, Versions, cell, percent, usd

Grades = list[list[TicketGrade]]


def decimal(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def signed(value: float, digits: int = 1) -> str:
    """Écart avec son signe, arrondi d'abord : « 0,0 » et jamais « +0,0 » ni « -0,0 »."""
    rounded = round(value, digits)
    if rounded == 0:
        return decimal(0, digits)
    return f"{'+' if rounded > 0 else ''}{decimal(rounded, digits)}"


LIMITS = f"""\
- Mêmes règles de notation et même vérité terrain pour les deux séries : elles sont renotées
  ici à partir de leurs résultats bruts, jamais relues dans un ancien rapport.
- Comparaison appariée : chaque ticket est comparé à lui-même, par son taux de réussite sur
  les passages de A puis de B. Test de signe exact, bilatéral, sur les tickets améliorés et
  dégradés (égalités écartées) ; « significatif » si p < {decimal(ALPHA, 2)}. Il faut au moins
  6 tickets qui changent, tous dans le même sens, pour l'être.
- Par catégorie (5 à 8 tickets), un écart n'est presque jamais significatif : il est
  indicatif seulement.
- Un ticket instable (son résultat change d'un passage à l'autre dans A ou dans B) pèse moins
  qu'il n'y paraît : son amélioration ou sa dégradation peut venir de la seule variabilité.
- Juge IA : non déterministe, hors score ; les deux séries sont jugées dans la même exécution,
  avec le même modèle et la même grille.
"""


def render(
    a: Sequence[Pass],
    b: Sequence[Pass],
    *,
    judge: JudgeComparison | None = None,
    compared_at: datetime,
    versions: Versions,
) -> str:
    grades_a = [p.grades for p in a]
    grades_b = [p.grades for p in b]
    name_a, name_b = a[0].report.summary.name, b[0].report.summary.name
    lines = [f"# Comparaison « {name_a} » (A) et « {name_b} » (B)", ""]
    lines += _header(a, b, compared_at, versions)
    lines += _overall(grades_a, grades_b, len(a), len(b))
    lines += _criteria(grades_a, grades_b)
    lines += _categories(grades_a, grades_b)
    lines += _tickets(grades_a, grades_b)
    lines += _costs(a, b, judge)
    if judge is not None:
        lines += _judge(judge)
    lines += ["## Limites de la comparaison", "", LIMITS]
    return "\n".join(lines)


def verdict(delta: Delta | JudgeDelta) -> str:
    """Significativité, avec le nombre de tickets qui changent."""
    p = decimal(delta.p_value, 3)
    moved = f"{delta.better} ↑ / {delta.worse} ↓"
    if delta.better + delta.worse == 0:
        return "aucun ticket ne change"
    word = "**significatif**" if delta.significant else "non significatif"
    return f"{word} ({moved}, p = {p})"


def points(delta: Delta) -> str:
    value = delta.points
    if value is None:
        return "—"
    return f"{signed(value, 0)} pts"


def _config(label: str, passes: Sequence[Pass]) -> str:
    summary = passes[0].report.summary
    code = summary.code
    models = sorted({m for p in passes for m in p.report.summary.models_used})
    dirty = " (modifications non commitées)" if code.dirty else ""
    files = ", ".join(f"`{p.path.name}`" for p in passes)
    return (
        f"- **{label}** : `{summary.name}`, consigne {summary.prompt_version} (empreinte "
        f"`{code.prompt_sha256[:12]}`), modèle demandé `{summary.model_requested}` "
        f"(utilisés : {', '.join(f'`{m}`' for m in models) or 'inconnus'}), commit "
        f"`{(code.commit or 'inconnu')[:12]}`{dirty}, {len(passes)} passage(s) : {files}"
    )


def _header(
    a: Sequence[Pass], b: Sequence[Pass], compared_at: datetime, versions: Versions
) -> list[str]:
    first_a, first_b = a[0].report.summary, b[0].report.summary
    differs = []
    if first_a.code.prompt_sha256 != first_b.code.prompt_sha256:
        differs.append(f"consigne ({first_a.prompt_version} → {first_b.prompt_version})")
    if first_a.model_requested != first_b.model_requested:
        differs.append(f"modèle ({first_a.model_requested} → {first_b.model_requested})")
    return [
        _config("A (référence)", a),
        _config("B (candidate)", b),
        f"- Ce qui change : {', '.join(differs)}",
        f"- Tickets : {len(first_a.tickets_requested)} ; date simulée "
        f"{first_a.simulation_now:%Y-%m-%d %H:%M} UTC",
        f"- Comparé le {compared_at:%Y-%m-%d %H:%M} UTC par `python -m evals.compare` : les deux "
        f"séries renotées avec la version de la notation `{versions.rules_sha256[:12]}` et la "
        f"vérité terrain `{versions.truth_sha256[:12]}`",
        "",
    ]


def _overall(grades_a: Grades, grades_b: Grades, passes_a: int, passes_b: int) -> list[str]:
    result = compare_overall(grades_a, grades_b)
    t = result.tickets
    return [
        "## Score global",
        "",
        "| | A | B | Écart | Significativité |",
        "|---|---|---|---|---|",
        f"| Tickets réussis (tous passages) | {t.a.passed} / {t.a.total} ({percent(t.a)}) | "
        f"{t.b.passed} / {t.b.total} ({percent(t.b)}) | {points(t)} | {verdict(t)} |",
        f"| pass^k (réussis à chaque passage) | {result.stable_a.passed} / "
        f"{result.stable_a.total} ({percent(result.stable_a)}, k = {passes_a}) | "
        f"{result.stable_b.passed} / {result.stable_b.total} ({percent(result.stable_b)}, "
        f"k = {passes_b}) | | |",
        "",
    ]


def _criteria(grades_a: Grades, grades_b: Grades) -> list[str]:
    lines = [
        "## Score par critère",
        "",
        "| Critère | A | B | Écart | Significativité |",
        "|---|---|---|---|---|",
    ]
    for criterion, d in compare_criteria(grades_a, grades_b).items():
        lines.append(
            f"| {CRITERIA_LABELS[criterion]} (`{criterion.value}`) | {d.a.passed} / {d.a.total} "
            f"({percent(d.a)}) | {d.b.passed} / {d.b.total} ({percent(d.b)}) | {points(d)} | "
            f"{verdict(d)} |"
        )
    return [*lines, ""]


def _categories(grades_a: Grades, grades_b: Grades) -> list[str]:
    lines = [
        "## Score par catégorie de ticket",
        "",
        "Indicatif : 5 à 8 tickets par catégorie.",
        "",
        "| Catégorie | A | B | Écart | Significativité |",
        "|---|---|---|---|---|",
    ]
    for category, d in compare_categories(grades_a, grades_b).items():
        lines.append(
            f"| {category} | {d.a.passed} / {d.a.total} ({percent(d.a)}) | {d.b.passed} / "
            f"{d.b.total} ({percent(d.b)}) | {points(d)} | {verdict(d)} |"
        )
    return [*lines, ""]


def _outcomes(outcomes: list[PassOutcome]) -> str:
    def one(o: PassOutcome) -> str:
        mark = "✓" if o.passed else "✗"
        failed = f" ({', '.join(c.value for c in o.failed)})" if o.failed else ""
        return f"p{o.pass_index} {mark}{failed}"

    return " ; ".join(one(o) for o in outcomes)


def _tickets(grades_a: Grades, grades_b: Grades) -> list[str]:
    changes = ticket_changes(grades_a, grades_b)
    improved = [c for c in changes if c.improved]
    degraded = [c for c in changes if not c.improved]
    lines = ["## Tickets améliorés et dégradés", ""]
    for title, items in (("Améliorés", improved), ("Dégradés", degraded)):
        lines += [f"### {title} ({len(items)})", ""]
        if not items:
            lines += ["Aucun.", ""]
            continue
        lines += [
            "| Ticket | Catégorie | A | B | Critères qui changent | Instable |",
            "|---|---|---|---|---|---|",
        ]
        for c in items:
            moved = ", ".join(f"`{x.value}`" for x in c.changes) or "—"
            lines.append(
                f"| {c.ticket_id} | {c.category} | {cell(_outcomes(c.outcomes_a))} | "
                f"{cell(_outcomes(c.outcomes_b))} | {moved} | {'oui' if c.unstable else ''} |"
            )
        lines.append("")
    return lines


def _costs(a: Sequence[Pass], b: Sequence[Pass], judge: JudgeComparison | None) -> list[str]:
    def series(passes: Sequence[Pass]) -> tuple[float, float | None, float | None]:
        total = sum(p.report.summary.total_cost_usd for p in passes)
        grades = [g for p in passes for g in p.grades]
        costs = [g.cost_usd for g in grades if g.cost_usd is not None]
        durations = [g.duration_ms / 1000 for g in grades if g.duration_ms is not None]
        return total, mean(costs) if costs else None, mean(durations) if durations else None

    total_a, cost_a, time_a = series(a)
    total_b, cost_b, time_b = series(b)

    def change(before: float | None, after: float | None) -> str:
        if not before or after is None:
            return "—"
        return f"{signed((after - before) / before * 100, 0)} %"

    def seconds(value: float | None) -> str:
        return "inconnue" if value is None else f"{decimal(value)} s"

    lines = [
        "## Coût et durée",
        "",
        "| | A | B | Écart |",
        "|---|---|---|---|",
        f"| Coût total (vérifications d'isolement comprises) | {usd(total_a)} | {usd(total_b)} | "
        f"{change(total_a, total_b)} |",
        f"| Coût moyen par ticket | {usd(cost_a)} | {usd(cost_b)} | {change(cost_a, cost_b)} |",
        f"| Durée moyenne par ticket | {seconds(time_a)} | {seconds(time_b)} | "
        f"{change(time_a, time_b)} |",
        "",
    ]
    if judge is not None:
        lines += [f"Juge IA (les deux séries) : {usd(judge.cost_usd)}.", ""]
    return lines


def _judge(judge: JudgeComparison) -> list[str]:
    def note(value: float | None) -> str:
        return "—" if value is None else decimal(value)

    def gap(d: JudgeDelta) -> str:
        if d.a is None or d.b is None:
            return "—"
        return signed(d.b - d.a)

    lines = [
        "## Juge IA : ton et clarté (facultatif)",
        "",
        "> Noté par un modèle, non déterministe : jamais compté dans le score.",
        "",
        "| Note sur 5 | A | B | Écart | Significativité (par ticket) |",
        "|---|---|---|---|---|",
        f"| Ton | {note(judge.tone.a)} | {note(judge.tone.b)} | {gap(judge.tone)} | "
        f"{verdict(judge.tone)} |",
        f"| Clarté | {note(judge.clarity.a)} | {note(judge.clarity.b)} | {gap(judge.clarity)} | "
        f"{verdict(judge.clarity)} |",
        "",
    ]
    if judge.skipped:
        lines += [f"Réponses non jugées (plafond atteint) : {judge.skipped}.", ""]
    lines += [
        f"Tickets dont une note moyenne bouge d'au moins {decimal(JUDGE_SHOWN_DELTA, 0)} points :",
        "",
    ]
    if not judge.moves:
        return [*lines, "Aucun.", ""]
    lines += ["| Ticket | Ton A → B | Clarté A → B |", "|---|---|---|"]
    for m in judge.moves:
        lines.append(
            f"| {m.ticket_id} | {decimal(m.tone_a)} → {decimal(m.tone_b)} | "
            f"{decimal(m.clarity_a)} → {decimal(m.clarity_b)} |"
        )
    return [*lines, ""]
