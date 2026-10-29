"""Comparaison de deux configurations (J5.3) : calculs, sans rendu ni appel réel.

Les deux séries sont notées avec les mêmes règles et la même vérité terrain
(`evals.compare` les renote à partir des résultats bruts). Chaque ticket est comparé à
lui-même (appariement) : son taux de réussite sur les passages de A, puis de B (0, ½ ou 1
avec deux passages). Amélioré si B > A, dégradé si B < A.

Significativité : test de signe exact, bilatéral, sur les tickets améliorés et dégradés
(égalités écartées). Il ne suppose rien de la distribution, reste exact sur de petits
échantillons, et s'explique en une phrase. Il faut au moins 6 tickets qui changent, tous dans
le même sens, pour passer sous 0,05.
"""

from collections.abc import Callable, Sequence
from math import comb

from pydantic import BaseModel, ConfigDict

from evals.grading import (
    Criterion,
    PassOutcome,
    Score,
    TicketGrade,
    Verdict,
    by_category,
    graded,
    outcome,
    overall,
    stable_passes,
    unstable,
)
from evals.judge import JudgeSummary

# Seuil de significativité du test de signe
ALPHA = 0.05
# Écart de note du juge (moyenne par ticket) montré ticket par ticket
JUDGE_SHOWN_DELTA = 2.0

Passes = Sequence[Sequence[TicketGrade]]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def sign_test(better: int, worse: int) -> float:
    """p-valeur bilatérale exacte du test de signe : 2 × P(X ≤ min) pour X ~ B(n, ½)."""
    n = better + worse
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(better, worse) + 1))
    return min(1.0, 2 * tail / 2**n)


class Delta(Strict):
    """Un score de A et de B, et le test de signe apparié par ticket."""

    a: Score
    b: Score
    better: int
    worse: int
    p_value: float

    @property
    def significant(self) -> bool:
        return self.p_value < ALPHA

    @property
    def points(self) -> float | None:
        """Écart des taux de réussite, en points de pourcentage."""
        if self.a.rate is None or self.b.rate is None:
            return None
        return (self.b.rate - self.a.rate) * 100


Verdicts = Callable[[TicketGrade], bool | None]


def rates(passes: Passes, passed: Verdicts) -> dict[int, float]:
    """Taux de réussite de chaque ticket sur les passages où il est noté (n/a écartés)."""
    seen: dict[int, list[bool]] = {}
    for grades in passes:
        for grade in graded(grades):
            value = passed(grade)
            if value is not None:
                seen.setdefault(grade.ticket_id, []).append(value)
    return {t: sum(v) / len(v) for t, v in seen.items()}


def score(passes: Passes, passed: Verdicts) -> Score:
    """Cas réussis sur tous les passages (même unité que les scores du rapport)."""
    values = [
        value
        for grades in passes
        for grade in graded(grades)
        if (value := passed(grade)) is not None
    ]
    return Score(passed=sum(values), total=len(values))


def delta(a: Passes, b: Passes, passed: Verdicts) -> Delta:
    rate_a, rate_b = rates(a, passed), rates(b, passed)
    common = rate_a.keys() & rate_b.keys()
    better = sum(rate_b[t] > rate_a[t] for t in common)
    worse = sum(rate_b[t] < rate_a[t] for t in common)
    return Delta(
        a=score(a, passed),
        b=score(b, passed),
        better=better,
        worse=worse,
        p_value=sign_test(better, worse),
    )


def ticket_passed(grade: TicketGrade) -> bool | None:
    return grade.passed


def criterion_passed(criterion: Criterion) -> Verdicts:
    def passed(grade: TicketGrade) -> bool | None:
        verdict = grade.verdict(criterion)
        if verdict is None or verdict is Verdict.NA:
            return None
        return verdict is Verdict.PASS

    return passed


def in_category(category: str) -> Verdicts:
    def passed(grade: TicketGrade) -> bool | None:
        return grade.passed if grade.category == category else None

    return passed


class Overall(Strict):
    tickets: Delta
    # pass^k : tickets réussis à chaque passage
    stable_a: Score
    stable_b: Score


def compare_overall(a: Passes, b: Passes) -> Overall:
    # Score global : même unité que `grading.overall` (tickets × passages)
    tickets = delta(a, b, ticket_passed).model_copy(
        update={
            "a": overall([g for grades in a for g in grades]),
            "b": overall([g for grades in b for g in grades]),
        }
    )
    return Overall(tickets=tickets, stable_a=stable_passes(a), stable_b=stable_passes(b))


def compare_criteria(a: Passes, b: Passes) -> dict[Criterion, Delta]:
    return {c: delta(a, b, criterion_passed(c)) for c in Criterion}


def compare_categories(a: Passes, b: Passes) -> dict[str, Delta]:
    categories = sorted(
        by_category([g for grades in a for g in grades]).keys()
        | by_category([g for grades in b for g in grades]).keys()
    )
    return {c: delta(a, b, in_category(c)) for c in categories}


class TicketChange(Strict):
    """Ticket amélioré ou dégradé : passages de A et de B, et ce qui change."""

    ticket_id: int
    category: str
    rate_a: float
    rate_b: float
    outcomes_a: list[PassOutcome]
    outcomes_b: list[PassOutcome]
    # Critères dont le taux de réussite change entre A et B
    changes: list[Criterion]
    # Résultat qui varie d'un passage à l'autre dans A ou dans B : écart moins sûr
    unstable: bool

    @property
    def improved(self) -> bool:
        return self.rate_b > self.rate_a


def _outcomes(passes: Passes, ticket_id: int) -> list[PassOutcome]:
    return [
        outcome(index, grade)
        for index, grades in enumerate(passes, start=1)
        for grade in graded(grades)
        if grade.ticket_id == ticket_id
    ]


def ticket_changes(a: Passes, b: Passes) -> list[TicketChange]:
    """Tickets améliorés puis dégradés, chacun par numéro."""
    rate_a, rate_b = rates(a, ticket_passed), rates(b, ticket_passed)
    shaky = {u.ticket_id for u in unstable(a)} | {u.ticket_id for u in unstable(b)}
    categories = {g.ticket_id: g.category for grades in [*a, *b] for g in graded(grades)}
    changes = []
    for ticket_id in sorted(rate_a.keys() & rate_b.keys()):
        if rate_a[ticket_id] == rate_b[ticket_id]:
            continue
        moved = []
        for criterion in Criterion:
            passed = criterion_passed(criterion)
            before = rates(a, passed).get(ticket_id)
            after = rates(b, passed).get(ticket_id)
            if before != after:
                moved.append(criterion)
        changes.append(
            TicketChange(
                ticket_id=ticket_id,
                category=categories[ticket_id],
                rate_a=rate_a[ticket_id],
                rate_b=rate_b[ticket_id],
                outcomes_a=_outcomes(a, ticket_id),
                outcomes_b=_outcomes(b, ticket_id),
                changes=moved,
                unstable=ticket_id in shaky,
            )
        )
    return sorted(changes, key=lambda c: (not c.improved, c.ticket_id))


# --- Juge IA -----------------------------------------------------------------------------


class JudgeMeans(Strict):
    """Notes moyennes d'une série, et par ticket (moyenne de ses passages jugés)."""

    tone: float | None
    clarity: float | None
    by_ticket: dict[int, tuple[float, float]]


def judge_means(summary: JudgeSummary) -> JudgeMeans:
    seen: dict[int, list[tuple[int, int]]] = {}
    for item in summary.items:
        if item.score is not None:
            seen.setdefault(item.ticket_id, []).append((item.score.tone, item.score.clarity))
    scores = [s for values in seen.values() for s in values]
    by_ticket = {
        t: (sum(v[0] for v in values) / len(values), sum(v[1] for v in values) / len(values))
        for t, values in sorted(seen.items())
    }
    return JudgeMeans(
        tone=sum(s[0] for s in scores) / len(scores) if scores else None,
        clarity=sum(s[1] for s in scores) / len(scores) if scores else None,
        by_ticket=by_ticket,
    )


class JudgeDelta(Strict):
    """Une note du juge (ton ou clarté) : moyennes A et B, test de signe par ticket."""

    a: float | None
    b: float | None
    better: int
    worse: int
    p_value: float

    @property
    def significant(self) -> bool:
        return self.p_value < ALPHA


class JudgeMove(Strict):
    ticket_id: int
    tone_a: float
    tone_b: float
    clarity_a: float
    clarity_b: float


class JudgeComparison(Strict):
    tone: JudgeDelta
    clarity: JudgeDelta
    # Tickets dont une note moyenne bouge d'au moins `JUDGE_SHOWN_DELTA`
    moves: list[JudgeMove]
    cost_usd: float
    skipped: int


def compare_judge(a: JudgeSummary, b: JudgeSummary) -> JudgeComparison:
    mean_a, mean_b = judge_means(a), judge_means(b)
    common = sorted(mean_a.by_ticket.keys() & mean_b.by_ticket.keys())

    def note(index: int, mean: float | None, other: float | None) -> JudgeDelta:
        better = sum(mean_b.by_ticket[t][index] > mean_a.by_ticket[t][index] for t in common)
        worse = sum(mean_b.by_ticket[t][index] < mean_a.by_ticket[t][index] for t in common)
        return JudgeDelta(
            a=mean, b=other, better=better, worse=worse, p_value=sign_test(better, worse)
        )

    moves = [
        JudgeMove(
            ticket_id=t,
            tone_a=mean_a.by_ticket[t][0],
            tone_b=mean_b.by_ticket[t][0],
            clarity_a=mean_a.by_ticket[t][1],
            clarity_b=mean_b.by_ticket[t][1],
        )
        for t in common
        if any(
            abs(mean_b.by_ticket[t][i] - mean_a.by_ticket[t][i]) >= JUDGE_SHOWN_DELTA
            for i in (0, 1)
        )
    ]
    return JudgeComparison(
        tone=note(0, mean_a.tone, mean_b.tone),
        clarity=note(1, mean_a.clarity, mean_b.clarity),
        moves=moves,
        cost_usd=a.cost_usd + b.cost_usd,
        skipped=a.skipped + b.skipped,
    )
