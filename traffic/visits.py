"""Parcours de clients simulés : ce qu'un client consulte, et dans quel ordre.

Le catalogue (clients et commandes) est découvert par l'API publique : clients sondés de 1 à N
jusqu'au premier 404 (les ids du seed sont contigus), puis leurs commandes. Les parcours ne
demandent que ce qui existe : pas de livraison pour une commande non expédiée. Seul le parcours
« numéro mal tapé » donne un 404, rarement : sans panne, le taux d'erreurs reste proche de 0.

Chaque parcours a sa trace (`traceparent`) : ses requêtes se suivent dans `shopsim.jsonl`.
"""

import functools
import itertools
import random
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from observability import trace
from traffic.client import ShopSimClient

# Statuts de commande qui ont une livraison (seed de ShopSim)
SHIPPED = frozenset({"shipped", "delivered"})
MAX_CUSTOMERS = 10_000
# Numéro de commande mal tapé : au-delà de tout id existant
MISTYPED_ORDER_OFFSET = 1_000_000


class DiscoveryError(Exception):
    """ShopSim ne répond pas normalement pendant la découverte (panne, service arrêté)."""


@dataclass(frozen=True)
class OrderRef:
    id: int
    customer_id: int
    status: str

    @property
    def shipped(self) -> bool:
        return self.status in SHIPPED


@dataclass
class Catalog:
    customers: list[int]
    orders: dict[int, list[OrderRef]]

    def all_orders(self) -> list[OrderRef]:
        return [order for orders in self.orders.values() for order in orders]

    def customers_with_orders(self) -> list[int]:
        return [customer for customer in self.customers if self.orders.get(customer)]


def discover(client: ShopSimClient) -> Catalog:
    """Clients et commandes de ShopSim ; lève `DiscoveryError` si ShopSim répond mal."""
    trace_id = trace.new_trace_id()
    customers: list[int] = []
    for customer_id in itertools.count(1):
        if customer_id > MAX_CUSTOMERS:
            break
        status, _ = client.get(f"/customers/{customer_id}", trace_id)
        if status == 404:
            break
        if status != 200:
            raise DiscoveryError(f"GET /customers/{customer_id} : {status}")
        customers.append(customer_id)
    orders: dict[int, list[OrderRef]] = {}
    for customer_id in customers:
        status, body = client.get(f"/customers/{customer_id}/orders", trace_id)
        if status != 200:
            raise DiscoveryError(f"GET /customers/{customer_id}/orders : {status}")
        orders[customer_id] = [
            OrderRef(order["id"], order["customer_id"], order["status"]) for order in body
        ]
    return Catalog(customers, orders)


@dataclass
class CatalogHolder:
    """Catalogue courant, remplacé à chaud par la relecture périodique."""

    catalog: Catalog
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self) -> Catalog:
        with self.lock:
            return self.catalog

    def set(self, catalog: Catalog) -> None:
        with self.lock:
            self.catalog = catalog


Journey = Callable[[Catalog, random.Random], list[str]]


def _customer(catalog: Catalog, rng: random.Random) -> int:
    return rng.choice(catalog.customers_with_orders())


def track_order(catalog: Catalog, rng: random.Random) -> list[str]:
    """« Où est mon colis ? » : compte, commandes, puis livraison d'une commande expédiée."""
    customer = _customer(catalog, rng)
    paths = [f"/customers/{customer}", f"/customers/{customer}/orders"]
    shipped = [order for order in catalog.orders[customer] if order.shipped]
    if shipped:
        paths.append(f"/shipments/{rng.choice(shipped).id}")
    return paths


def check_payment(catalog: Catalog, rng: random.Random) -> list[str]:
    """« Mon paiement est-il passé ? » : commande puis paiement."""
    order = rng.choice(catalog.orders[_customer(catalog, rng)])
    return [f"/orders/{order.id}", f"/payments/{order.id}"]


def browse_account(catalog: Catalog, rng: random.Random) -> list[str]:
    """Consultation du compte et de l'historique des commandes."""
    customer = _customer(catalog, rng)
    return [f"/customers/{customer}", f"/customers/{customer}/orders"]


def review_order(catalog: Catalog, rng: random.Random) -> list[str]:
    """Détail complet d'une commande : commande, paiement, livraison si expédiée."""
    order = rng.choice(catalog.orders[_customer(catalog, rng)])
    paths = [f"/orders/{order.id}", f"/payments/{order.id}"]
    if order.shipped:
        paths.append(f"/shipments/{order.id}")
    return paths


def mistyped_order(catalog: Catalog, rng: random.Random) -> list[str]:
    """Numéro de commande mal tapé : un 404 voulu, rare."""
    return [f"/orders/{MISTYPED_ORDER_OFFSET + rng.randint(1, 999_999)}"]


# Poids des parcours (le mélange de routes en découle)
JOURNEYS: tuple[tuple[Journey, int], ...] = (
    (track_order, 35),
    (check_payment, 25),
    (browse_account, 19),
    (review_order, 19),
    (mistyped_order, 2),
)


def requests(
    holder: CatalogHolder, client: ShopSimClient, rng: random.Random
) -> Iterator[Callable[[], object]]:
    """Flux infini de requêtes : les parcours tirés au sort, mis bout à bout, une trace chacun."""
    journeys = [journey for journey, _ in JOURNEYS]
    weights = [weight for _, weight in JOURNEYS]
    while True:
        journey = rng.choices(journeys, weights)[0]
        trace_id = trace.new_trace_id()
        for path in journey(holder.get(), rng):
            # Liés tout de suite : la requête part plus tard, pendant un autre parcours
            yield functools.partial(client.get, path, trace_id)  # type: ignore[misc]
