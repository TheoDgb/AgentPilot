"""Nouveaux tickets de clients simulés, désactivés par défaut.

Chaque ticket créé est traité par l'agent Support et coûte de l'argent : le générateur n'en crée
que si on le demande (`--tickets-per-hour`), au plus `MAX_PER_HOUR` par heure glissante, et au
plus `--max-tickets` au total.

Un ticket est cohérent avec les données : le client parle d'une de ses propres commandes, jamais
déjà citée par un ticket ouvert, et le texte vient de la famille qui correspond à l'état de la
commande lu par l'API (livraison en retard, paiement non confirmé, commande livrée, question).
Textes et formats des modèles du seed (`shopsim/ticket_templates.py`). Jamais de piège
(injection, commande d'un autre client, montant réclamé) : ce sont des clients ordinaires.

Pas de vérité terrain : ces tickets sont hors évaluation (`python -m agentpilot.reset` les
efface avant une évaluation).
"""

import random
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from observability import trace
from shopsim import ticket_templates as tpl
from traffic.client import ShopSimClient
from traffic.visits import Catalog

MAX_PER_HOUR = 60
HOUR_S = 3600.0
OPEN_PAGE = 100
# Livraison pas encore arrivée
MOVING = frozenset({"label_created", "in_transit", "out_for_delivery"})


class Family:
    DELAY = "delay"
    PAYMENT = "payment_not_confirmed"
    REFUND = "refund_request"
    DEFECTIVE = "defective_product"
    QUESTION = "question_order"


@dataclass(frozen=True)
class Draft:
    customer_id: int
    order_id: int
    family: str
    subject: str
    body: str


def _day(value: str) -> date:
    return datetime.fromisoformat(value).date()


def choose_template(
    order: dict[str, Any],
    shipment: dict[str, Any] | None,
    today: date,
    rng: random.Random,
) -> tuple[str, tpl.Template]:
    """Famille et modèle de texte qui correspondent à l'état de la commande."""
    if shipment is not None and (
        shipment["status"] in ("delayed", "lost")
        or (shipment["status"] in MOVING and _day(shipment["estimated_delivery"]) < today)
    ):
        return Family.DELAY, rng.choice(tpl.DELAY)
    if order["status"] in ("pending", "cancelled"):
        return Family.PAYMENT, rng.choice(tpl.PAYMENT_NOT_CONFIRMED)
    if shipment is not None and shipment["status"] == "delivered":
        if rng.random() < 0.5:
            return Family.REFUND, rng.choice(tpl.REFUND_REQUEST)
        return Family.DEFECTIVE, rng.choice(tpl.DEFECTIVE_PRODUCT)
    # Question simple : variante selon l'expédition, comme dans le seed
    return Family.QUESTION, tpl.QUESTION_ORDER[0 if shipment is None else 1]


def context(
    first_name: str, order: dict[str, Any], shipment: dict[str, Any] | None, now: datetime
) -> dict[str, str | int]:
    """Champs des modèles, mêmes formats que le seed (`seed_tickets._context`)."""
    ctx: dict[str, str | int] = {
        "first_name": first_name,
        "order_id": order["id"],
        "total": tpl.euros(order["total_cents"]),
        "days_since_order": (now - datetime.fromisoformat(order["created_at"])).days,
    }
    if shipment is not None:
        ctx |= {
            "carrier": shipment["carrier"],
            "tracking_number": shipment["tracking_number"],
            "estimated": tpl.fr_date(_day(shipment["estimated_delivery"])),
        }
        if shipment["delivered_at"] is not None:
            ctx["delivered"] = tpl.fr_date(_day(shipment["delivered_at"]))
    return ctx


def cited_orders(client: ShopSimClient, trace_id: str) -> set[int] | None:
    """Commandes déjà citées par un ticket ouvert ; None si ShopSim répond mal."""
    cited: set[int] = set()
    offset = 0
    while True:
        status, page = client.get(
            f"/tickets?status=open&limit={OPEN_PAGE}&offset={offset}", trace_id
        )
        if status != 200:
            return None
        cited |= {t["order_id"] for t in page["items"] if t["order_id"] is not None}
        offset += OPEN_PAGE
        if offset >= page["total"]:
            return cited


def draft_ticket(client: ShopSimClient, catalog: Catalog, rng: random.Random) -> Draft | None:
    """Brouillon d'un ticket cohérent avec les données ; None si rien ne convient ou si ShopSim
    répond mal (le prochain essai recommencera)."""
    trace_id = trace.new_trace_id()
    cited = cited_orders(client, trace_id)
    if cited is None:
        return None
    candidates = [o for o in catalog.all_orders() if o.id not in cited and o.status != "refunded"]
    if not candidates:
        return None
    picked = rng.choice(candidates)
    reads = [
        client.get(f"/customers/{picked.customer_id}", trace_id),
        client.get(f"/orders/{picked.id}", trace_id),
        client.get("/clock", trace_id),
    ]
    if any(status != 200 for status, _ in reads):
        return None
    (_, customer), (_, order), (_, clock) = reads
    # Relu : le catalogue a pu vieillir
    if order["customer_id"] != picked.customer_id or order["status"] == "refunded":
        return None
    shipment = None
    if order["status"] in ("shipped", "delivered"):
        status, shipment = client.get(f"/shipments/{order['id']}", trace_id)
        if status != 200:
            return None
    now = datetime.fromisoformat(clock["now"])
    family, template = choose_template(order, shipment, now.date(), rng)
    fields = context(customer["first_name"], order, shipment, now)
    subject, body = (part.format(**fields) for part in template)
    return Draft(customer["id"], order["id"], family, subject, body)


def create_ticket(client: ShopSimClient, draft: Draft) -> int | None:
    """Crée le ticket (clé d'idempotence propre) ; renvoie son id, None en cas d'échec."""
    status, ticket = client.request(
        "POST",
        "/tickets",
        payload={
            "customer_id": draft.customer_id,
            "order_id": draft.order_id,
            "subject": draft.subject,
            "body": draft.body,
        },
        headers={"Idempotency-Key": f"traffic-{uuid.uuid4()}"},
        trace_id=trace.new_trace_id(),
    )
    if status not in (200, 201):
        return None
    client.stats.ticket()
    return int(ticket["id"])


class TicketScheduler:
    """Intervalles aléatoires (loi exponentielle) autour du débit demandé, plafonné à
    `MAX_PER_HOUR` par heure glissante, et `max_tickets` au total."""

    def __init__(
        self,
        per_hour: float,
        rng: random.Random,
        *,
        max_tickets: int | None = None,
        clock: Callable[[], float],
        wait: Callable[[float], bool],
    ) -> None:
        self.per_hour = min(per_hour, MAX_PER_HOUR)
        self.rng = rng
        self.max_tickets = max_tickets
        self.clock = clock
        # Attend ; True s'il faut s'arrêter
        self.wait = wait
        # Dates de création dans l'heure glissante
        self.recent: deque[float] = deque()
        self.count = 0

    def _done(self) -> bool:
        return self.max_tickets is not None and self.count >= self.max_tickets

    def run(self, create_one: Callable[[], bool]) -> None:
        """Crée des tickets jusqu'à l'arrêt ou `max_tickets` ; `create_one` : True si créé."""
        if self.per_hour <= 0:
            return
        while not self._done():
            if self.wait(self.rng.expovariate(self.per_hour / HOUR_S)):
                return
            while self.recent and self.clock() - self.recent[0] >= HOUR_S:
                self.recent.popleft()
            # Heure glissante pleine : attendre que le plus ancien en sorte
            if len(self.recent) >= MAX_PER_HOUR:
                if self.wait(HOUR_S - (self.clock() - self.recent[0])):
                    return
                self.recent.popleft()
            if create_one():
                self.recent.append(self.clock())
                self.count += 1
