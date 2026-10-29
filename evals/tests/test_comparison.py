"""Comparaison de deux séries (`evals/comparison.py`) : appariement par ticket, test de signe."""

import pytest

from evals.comparison import (
    compare_categories,
    compare_criteria,
    compare_judge,
    compare_overall,
    delta,
    sign_test,
    ticket_changes,
    ticket_passed,
)
from evals.grading import Criterion, CriterionResult, TicketGrade, Verdict
from evals.judge import JudgedReply, JudgeScore, JudgeSummary
from evals.results import TicketStatus


def grade(
    ticket_id: int,
    passed: bool,
    *,
    category: str = "refund_request",
    failed: tuple[Criterion, ...] = (Criterion.ACTION,),
    na: tuple[Criterion, ...] = (),
) -> TicketGrade:
    """Ticket noté sur trois critères ; en échec sur `failed` s'il n'est pas réussi."""
    criteria = []
    for criterion in (Criterion.CATEGORY, Criterion.ACTION, Criterion.FACTS):
        if criterion in na:
            verdict = Verdict.NA
        elif not passed and criterion in failed:
            verdict = Verdict.FAIL
        else:
            verdict = Verdict.PASS
        criteria.append(CriterionResult(criterion=criterion, verdict=verdict))
    return TicketGrade(
        ticket_id=ticket_id,
        category=category,
        expected_action="refund",
        status=TicketStatus.SUCCEEDED,
        passed=passed,
        criteria=criteria,
    )


def series(*passes: dict[int, bool]) -> list[list[TicketGrade]]:
    return [[grade(t, ok) for t, ok in p.items()] for p in passes]


@pytest.mark.parametrize(
    ("better", "worse", "p_value"),
    [
        (6, 0, 0.03125),
        (0, 6, 0.03125),
        (9, 1, 0.021484375),
        (5, 0, 0.0625),
        (3, 2, 1.0),
        (0, 0, 1.0),
    ],
)
def test_sign_test(better: int, worse: int, p_value: float) -> None:
    assert sign_test(better, worse) == pytest.approx(p_value)


def test_six_tickets_improved_and_none_degraded_is_significant() -> None:
    a = series({t: False for t in range(1, 7)} | {7: True})
    b = series({t: True for t in range(1, 8)})
    d = delta(a, b, ticket_passed)
    assert (d.better, d.worse) == (6, 0)
    assert d.significant and d.points == pytest.approx(600 / 7)


def test_small_differences_are_not_significant() -> None:
    a = series({1: False, 2: False, 3: False, 4: True, 5: True})
    b = series({1: True, 2: True, 3: True, 4: False, 5: False})
    d = delta(a, b, ticket_passed)
    assert (d.better, d.worse) == (3, 2) and not d.significant
    same = delta(a, a, ticket_passed)
    assert (same.better, same.worse, same.p_value) == (0, 0, 1.0)


def test_paired_by_ticket_rate_over_the_passes() -> None:
    """Deux passages : un ticket passe de ½ (instable) à 1, un autre de 1 à ½."""
    a = series({1: False, 2: True}, {1: True, 2: True})
    b = series({1: True, 2: True}, {1: True, 2: False})
    result = compare_overall(a, b)
    assert (result.tickets.better, result.tickets.worse) == (1, 1)
    # Score global : tickets × passages, comme le rapport d'une série
    assert (result.tickets.a.passed, result.tickets.a.total) == (3, 4)
    assert (result.stable_a.passed, result.stable_a.total) == (1, 2)


def test_ticket_changes_improved_first_with_criteria_and_instability() -> None:
    a = [
        [grade(1, False), grade(2, True), grade(3, True), grade(4, False)],
        [grade(1, False), grade(2, True), grade(3, False), grade(4, False)],
    ]
    b = [
        [grade(1, True), grade(2, False, failed=(Criterion.FACTS,)), grade(3, True)]
        + [grade(4, False)],
        [grade(1, True), grade(2, True), grade(3, True), grade(4, False)],
    ]
    changes = ticket_changes(a, b)
    assert [(c.ticket_id, c.improved) for c in changes] == [(1, True), (3, True), (2, False)]
    first, third, second = changes
    assert first.changes == [Criterion.ACTION] and not first.unstable
    assert [o.passed for o in first.outcomes_a] == [False, False]
    # Ticket 3 instable dans A, ticket 2 dans B : écart moins sûr
    assert third.unstable and second.unstable
    assert second.changes == [Criterion.FACTS]
    # Ticket 4 : en échec partout, ni amélioré ni dégradé
    assert 4 not in {c.ticket_id for c in changes}


def test_criteria_not_applicable_are_left_out() -> None:
    a = [[grade(1, True, na=(Criterion.FACTS,)), grade(2, False, failed=(Criterion.FACTS,))]]
    b = [[grade(1, True, na=(Criterion.FACTS,)), grade(2, True)]]
    facts = compare_criteria(a, b)[Criterion.FACTS]
    assert (facts.a.passed, facts.a.total, facts.b.passed, facts.b.total) == (0, 1, 1, 1)
    assert (facts.better, facts.worse) == (1, 0)
    assert compare_criteria(a, b)[Criterion.AMOUNT].a.total == 0


def test_categories_compared_separately() -> None:
    a = [[grade(1, False, category="x"), grade(2, True, category="y")]]
    b = [[grade(1, True, category="x"), grade(2, True, category="y")]]
    categories = compare_categories(a, b)
    assert list(categories) == ["x", "y"]
    assert (categories["x"].better, categories["y"].better) == (1, 0)


def judged(*notes: tuple[int, int, int, int]) -> JudgeSummary:
    """(ticket, passage, ton, clarté)"""
    return JudgeSummary(
        model="haiku",
        rubric_sha256="r" * 64,
        max_usd=0.5,
        cost_usd=0.01,
        items=[
            JudgedReply(
                ticket_id=t,
                pass_index=p,
                score=JudgeScore(tone=tone, clarity=clarity, justification="ok"),
            )
            for t, p, tone, clarity in notes
        ],
    )


def test_judge_means_sign_test_and_big_moves() -> None:
    a = judged((1, 1, 2, 4), (1, 2, 2, 4), (2, 1, 3, 3), (3, 1, 4, 4))
    b = judged((1, 1, 4, 4), (1, 2, 5, 4), (2, 1, 4, 3), (3, 1, 4, 2))
    result = compare_judge(a, b)
    assert result.tone.a == pytest.approx(2.75) and result.tone.b == pytest.approx(4.25)
    assert (result.tone.better, result.tone.worse) == (2, 0) and not result.tone.significant
    assert (result.clarity.better, result.clarity.worse) == (0, 1)
    # Ticket 1 : ton 2 → 4,5 ; ticket 3 : clarté 4 → 2
    assert [m.ticket_id for m in result.moves] == [1, 3]
    assert result.cost_usd == pytest.approx(0.02)
