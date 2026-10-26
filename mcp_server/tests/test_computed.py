"""Valeurs calculées : mêmes réponses que les règles de `evals/policy.py`."""

from datetime import UTC, date, datetime, timedelta

import pytest

from evals.policy import REFUND_WINDOW, RISK_WINDOW, is_risky_customer, within_refund_window
from mcp_server import computed

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
# Décalages autour des limites, à la seconde près
OFFSETS = [timedelta(seconds=s) for s in (-1, 0, 1)] + [timedelta(hours=h) for h in (-12, 12)]


@pytest.mark.parametrize("offset", OFFSETS)
def test_days_since_delivery_matches_refund_window(offset: timedelta) -> None:
    """« Plus de 30 jours entamés » équivaut exactement à « hors délai de remboursement »."""
    delivered_at = NOW - REFUND_WINDOW - offset

    over = computed.days_since(delivered_at, NOW) > REFUND_WINDOW.days

    assert over == (not within_refund_window(delivered_at, NOW))


@pytest.mark.parametrize("offset", OFFSETS)
def test_refunds_in_risk_window_matches_policy(offset: timedelta) -> None:
    edge = NOW - RISK_WINDOW - offset
    recent = [NOW - timedelta(days=1), NOW - timedelta(days=10)]

    count = computed.refunds_in_risk_window([edge, *recent], NOW)

    assert count == sum(timedelta(0) <= NOW - at <= RISK_WINDOW for at in [edge, *recent])
    assert (count >= 3) == is_risky_customer([edge, *recent], NOW)


def test_days_since_counts_started_days() -> None:
    assert computed.days_since(NOW - timedelta(hours=2), NOW) == 1
    assert computed.days_since(NOW - timedelta(days=3), NOW) == 3
    assert computed.days_since(NOW, NOW) == 0
    # Jamais négatif (date future)
    assert computed.days_since(NOW + timedelta(days=1), NOW) == 0


def test_days_past_estimated_delivery() -> None:
    assert computed.days_past(date(2026, 7, 3), NOW) == 3
    assert computed.days_past(date(2026, 7, 6), NOW) == 0
    assert computed.days_past(date(2026, 7, 9), NOW) == 0


def test_risk_window_days_comes_from_policy() -> None:
    assert computed.customer_values([], NOW)["risk_window_days"] == RISK_WINDOW.days
