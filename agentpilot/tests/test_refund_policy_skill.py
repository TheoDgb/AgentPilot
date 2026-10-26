"""La skill refund-policy reprend exactement les valeurs de `evals/policy.py`."""

import re

from agentpilot.run_support import SKILL_PATH, skill_body
from evals.policy import APPROVAL_THRESHOLD_CENTS, REFUND_WINDOW, RISK_MIN_REFUNDS, RISK_WINDOW

SKILL = SKILL_PATH.read_text(encoding="utf-8")


def numbers(pattern: str) -> set[int]:
    return {int(n) for n in re.findall(pattern, SKILL)}


def test_skill_has_a_header() -> None:
    assert SKILL.startswith("---\nname: refund-policy\ndescription: ")
    assert skill_body(SKILL).startswith("# Politique de remboursement")


def test_durations_match_policy() -> None:
    """Tous les « N jours » de la skill : délai de remboursement et fenêtre de risque."""
    assert numbers(r"(\d+)\s+(?:derniers\s+)?jours") == {REFUND_WINDOW.days, RISK_WINDOW.days}


def test_approval_threshold_matches_policy() -> None:
    assert numbers(r"(\d+)\s*€") == {APPROVAL_THRESHOLD_CENTS // 100}
    assert numbers(r"`amount_cents`\s*>\s*(\d+)") == {APPROVAL_THRESHOLD_CENTS}


def test_risky_customer_rule_matches_policy() -> None:
    assert numbers(r"(\d+)\s+remboursements") == {RISK_MIN_REFUNDS}
    assert f"{RISK_MIN_REFUNDS} remboursements sur les {RISK_WINDOW.days} derniers jours" in SKILL
