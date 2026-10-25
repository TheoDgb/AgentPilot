"""Latence simulée du service Paiements.

La latence normale vient de `PAYMENT_LATENCY_MS`, relue à chaque appel. Pendant
la panne Chaos `payment_latency`, elle passe à 4 s (voir `shopsim/app/chaos.py`).
"""

import os
import time

from sqlalchemy.orm import Session

from shopsim.app.chaos import CHAOS_PAYMENT_LATENCY_MS, is_active
from shopsim.app.models import ChaosScenario

DEFAULT_PAYMENT_LATENCY_MS = 100


def payment_latency_ms(session: Session) -> int:
    if is_active(session, ChaosScenario.PAYMENT_LATENCY):
        return CHAOS_PAYMENT_LATENCY_MS
    return int(os.environ.get("PAYMENT_LATENCY_MS", DEFAULT_PAYMENT_LATENCY_MS))


def simulate_payment_latency(session: Session) -> None:
    time.sleep(payment_latency_ms(session) / 1000)
