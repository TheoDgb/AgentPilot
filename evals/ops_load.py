"""Charge de remboursements de l'évaluation de l'agent Ops (scénario `bad_deploy`).

    python -m evals.ops_load --per-minute 12 --orders 1,2,3

Envoie des `POST /refunds` valides à ShopSim, un à la fois, pendant la panne : ils échouent en
500, rien n'est remboursé. Seule l'évaluation envoie ces remboursements, jamais un agent.

Arrêt (SIGTERM ou SIGINT) : plus aucun nouvel envoi, l'envoi en cours va jusqu'au bout, puis
le processus quitte en écrivant le nombre d'envois sur stdout. Le lanceur (ou son gardien)
attend cette fin **avant** de désactiver la panne : un remboursement parti après la fin de la
panne réussirait et modifierait les données.
"""

import argparse
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from types import FrameType

import httpx

DEFAULT_URL = "http://localhost:8000"
TIMEOUT_S = 10.0
AMOUNT_CENTS = 100
# Pas d'attente entre deux contrôles de l'arrêt
TICK_S = 0.2

Send = Callable[[str, str, dict[str, object] | None, dict[str, str] | None], int]


class Load:
    """Envois un à un ; `stopping` passe à vrai sur un signal, l'envoi en cours finit."""

    def __init__(self, send: Send, per_minute: int, orders: Sequence[int]) -> None:
        self.send = send
        self.interval_s = 60 / per_minute
        self.orders = list(orders)
        self.stopping = False
        self.sent = 0

    def stop(self, signum: int = 0, frame: FrameType | None = None) -> None:
        self.stopping = True

    def _payment(self, order_id: int) -> int | None:
        """Identifiant du paiement de la commande (le GET le renvoie), sinon None."""
        payment_id = self.send("GET", f"/payments/{order_id}", None, None)
        return payment_id or None

    def run(self, sleep: Callable[[float], None] = time.sleep) -> int:
        index = 0
        while not self.stopping:
            order_id = self.orders[index % len(self.orders)]
            index += 1
            # Paiement de la commande ; une commande sans paiement est sautée
            payment_id = self._payment(order_id)
            if payment_id is not None and not self.stopping:
                self.send(
                    "POST",
                    "/refunds",
                    {"payment_id": payment_id, "amount_cents": AMOUNT_CENTS, "reason": "Retour"},
                    {"Idempotency-Key": f"ops-eval-{uuid.uuid4()}"},
                )
                self.sent += 1
            waited = 0.0
            while waited < self.interval_s and not self.stopping:
                sleep(TICK_S)
                waited += TICK_S
        return self.sent


def http_sender(base_url: str) -> Send:
    client = httpx.Client(base_url=base_url, timeout=TIMEOUT_S)

    def send(
        method: str, path: str, body: dict[str, object] | None, headers: dict[str, str] | None
    ) -> int:
        """GET : identifiant du paiement (ou 0) ; POST : code HTTP (0 si injoignable)."""
        try:
            response = client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError:
            return 0
        if method == "GET":
            if response.status_code != 200:
                return 0
            return int(response.json().get("id", 0))
        return response.status_code

    return send


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.ops_load")
    parser.add_argument("--per-minute", type=int, required=True)
    parser.add_argument("--orders", required=True, help="commandes payées, ex. 1,2,3")
    args = parser.parse_args(argv)
    orders = [int(o) for o in args.orders.split(",") if o]
    load = Load(http_sender(os.environ.get("SHOPSIM_URL") or DEFAULT_URL), args.per_minute, orders)
    signal.signal(signal.SIGTERM, load.stop)
    signal.signal(signal.SIGINT, load.stop)
    sent = load.run()
    print(json.dumps({"sent": sent}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
