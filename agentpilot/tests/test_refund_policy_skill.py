"""La skill refund-policy, dans chaque version de la consigne, reprend exactement les valeurs de
`evals/policy.py` (et la v2, les transporteurs du seed)."""

import re

import pytest

from agentpilot.support_agent import DEFAULT_PROMPT, PROMPTS, SKILL_PATH, skill_body
from evals.policy import (
    APPROVAL_THRESHOLD_CENTS,
    HUMAN_FOLLOW_UP_HOURS,
    REFUND_WINDOW,
    RISK_MIN_REFUNDS,
    RISK_WINDOW,
)
from shopsim.seed import CARRIERS

SKILLS = {version: files.skill.read_text(encoding="utf-8") for version, files in PROMPTS.items()}
V2 = SKILLS["v2"]


def numbers(pattern: str, text: str) -> set[int]:
    return {int(n) for n in re.findall(pattern, text)}


def test_v1_skill_is_kept_where_it_was() -> None:
    """La v1 reste à sa place pour comparer ; le service utilise la v2 depuis J5.3."""
    assert PROMPTS["v1"].skill == SKILL_PATH
    assert DEFAULT_PROMPT == "v2"


@pytest.mark.parametrize("version", sorted(SKILLS))
def test_skill_has_a_header(version: str) -> None:
    skill = SKILLS[version]
    assert skill.startswith("---\nname: refund-policy\ndescription: ")
    assert skill_body(skill).startswith("# Politique de remboursement")


@pytest.mark.parametrize("version", sorted(SKILLS))
def test_durations_match_policy(version: str) -> None:
    """Tous les « N jours » de la skill : délai de remboursement et fenêtre de risque."""
    found = numbers(r"(\d+)\s+(?:derniers\s+)?jours", SKILLS[version])
    assert found == {REFUND_WINDOW.days, RISK_WINDOW.days}


@pytest.mark.parametrize("version", sorted(SKILLS))
def test_approval_threshold_matches_policy(version: str) -> None:
    assert numbers(r"(\d+)\s*€", SKILLS[version]) == {APPROVAL_THRESHOLD_CENTS // 100}
    assert numbers(r"`amount_cents`\s*>\s*(\d+)", SKILLS[version]) == {APPROVAL_THRESHOLD_CENTS}


@pytest.mark.parametrize("version", sorted(SKILLS))
def test_risky_customer_rule_matches_policy(version: str) -> None:
    skill = SKILLS[version]
    assert numbers(r"(\d+)\s+remboursements", skill) == {RISK_MIN_REFUNDS}
    assert f"{RISK_MIN_REFUNDS} remboursements sur les {RISK_WINDOW.days} derniers jours" in skill


def test_v2_follow_up_delay_matches_policy() -> None:
    assert numbers(r"(\d+)\s*h\b", V2) == {HUMAN_FOLLOW_UP_HOURS}
    assert f"sous {HUMAN_FOLLOW_UP_HOURS} h ouvrées" in V2
    # La v1 reste celle de la référence : aucun délai annoncé
    assert numbers(r"(\d+)\s*h\b", SKILLS["v1"]) == set()


def test_v2_carriers_are_the_seed_carriers() -> None:
    [line] = [x for x in V2.splitlines() if x.startswith("Lumen livre avec")]
    listed = set(re.findall(r"\b[A-Z]\w+", line.split(":", 1)[1].split(".", 1)[0]))
    assert listed == set(CARRIERS)


def test_v2_compares_the_claimed_amount_to_the_amount_paid_before_the_deadline() -> None:
    """Tickets 44 et 45 de « baseline » : refus hors délai au lieu d'escalader."""
    body = " ".join(V2.split())
    trap = body.index("Montant réclamé supérieur au montant payé")
    assert trap < body.index("**Remboursement hors délai**")
    assert "`amount_cents` de `get_payment`" in body
    assert "hors délai dont le client réclame plus que le montant payé est un cas piège" in body
