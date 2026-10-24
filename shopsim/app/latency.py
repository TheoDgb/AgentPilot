"""Latence simulée du service Paiements.

La valeur est relue à chaque appel pour que l'endpoint Chaos (J1.5) puisse la
modifier à chaud.
"""

import os
import time

DEFAULT_PAYMENT_LATENCY_MS = 100


def payment_latency_ms() -> int:
    return int(os.environ.get("PAYMENT_LATENCY_MS", DEFAULT_PAYMENT_LATENCY_MS))


def simulate_payment_latency() -> None:
    time.sleep(payment_latency_ms() / 1000)
