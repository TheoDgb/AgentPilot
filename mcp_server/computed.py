"""Valeurs calculées ajoutées aux outils de lecture : le code calcule, le modèle juge.

Les calculs se font à la date de l'horloge de ShopSim (`GET /clock`), la même pour tous
(figée à 2026-07-06 12:00 pendant les évaluations). Aucun seuil de politique ici : on
donne des nombres, la skill de l'agent dit quoi en conclure. Les seules règles reprises
viennent de `evals/policy.py`, pour que les comptes collent à la vérité terrain.
"""

import math
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from evals.policy import RISK_WINDOW

DAY = timedelta(days=1)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def days_since(moment: datetime, now: datetime) -> int:
    """Jours entamés depuis `moment` : « plus de N » équivaut à « plus de N × 24 h »,
    comme `within_refund_window` de `evals/policy.py`."""
    return max(0, math.ceil((now - moment) / DAY))


def days_past(deadline: date, now: datetime) -> int:
    """Jours de retard sur une date prévue ; 0 si elle n'est pas encore dépassée."""
    return max(0, (now.date() - deadline).days)


def refunds_in_risk_window(refund_dates: Iterable[datetime], now: datetime) -> int:
    """Même condition que `is_risky_customer` de `evals/policy.py`."""
    return sum(timedelta(0) <= now - at <= RISK_WINDOW for at in refund_dates)


def shipment_values(shipment: dict[str, Any], now: datetime) -> dict[str, Any]:
    delivered_at = shipment.get("delivered_at")
    return {
        "as_of": now.isoformat(),
        "days_since_delivery": (
            days_since(parse_datetime(delivered_at), now) if delivered_at else None
        ),
        "days_past_estimated_delivery": days_past(
            date.fromisoformat(shipment["estimated_delivery"]), now
        ),
    }


def payment_values(payment: dict[str, Any]) -> dict[str, Any]:
    return {"remaining_refundable_cents": payment["amount_cents"] - payment["refunded_cents"]}


def customer_values(refunds: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    dates = [parse_datetime(refund["created_at"]) for refund in refunds]
    return {
        "as_of": now.isoformat(),
        "refunds_in_risk_window": refunds_in_risk_window(dates, now),
        "risk_window_days": RISK_WINDOW.days,
    }
