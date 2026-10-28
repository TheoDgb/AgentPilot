"""Parcours de clients : contre la vraie ShopSim de test, seulement les routes attendues, aucun
appel au Chaos ni aux actions, pas d'erreur hors des numéros mal tapés."""

import random
from collections import Counter
from collections.abc import Mapping
from itertools import islice
from typing import Any

import pytest

from observability.metrics import HTTP_REGISTRY
from observability.trace import parse_traceparent
from traffic import visits
from traffic.client import ShopSimClient
from traffic.tests.conftest import Send, Shop
from traffic.visits import CatalogHolder, DiscoveryError, discover, requests

READ_ROUTES = {
    "/customers/{customer_id}",
    "/customers/{customer_id}/orders",
    "/orders/{order_id}",
    "/payments/{order_id}",
    "/shipments/{order_id}",
}
N_REQUESTS = 400


def http_counts() -> dict[tuple[str, str, str], float]:
    """Requêtes vues par ShopSim (métriques), par méthode, gabarit et code."""
    return {
        (s.labels["method"], s.labels["route"], s.labels["status"]): s.value
        for family in HTTP_REGISTRY.collect()
        for s in family.samples
        if s.name == "http_requests_total"
    }


def test_journeys_use_only_read_routes_that_exist(send: Send, shop: Shop) -> None:
    client = ShopSimClient(send)
    before = http_counts()

    for task in islice(requests(CatalogHolder(shop.catalog), client, random.Random(7)), N_REQUESTS):
        task()

    counts, _, _ = client.stats.take()
    by_route: Counter[str] = Counter()
    for key, count in counts.items():
        method, route, status = key.split(" ")
        assert method == "GET"
        assert route in READ_ROUTES
        # Seul le numéro mal tapé échoue, et seulement sur /orders
        assert status == "200" or (status == "404" and route == "/orders/{order_id}"), key
        by_route[route] += count
    assert sum(by_route.values()) == N_REQUESTS
    assert set(by_route) == READ_ROUTES
    not_found = counts.get("GET /orders/{order_id} 404", 0)
    assert 0 < not_found <= N_REQUESTS * 0.05

    # Vu par ShopSim : seulement des GET sur ces routes, rien sur les actions ni Chaos
    after = http_counts()
    changed = {key for key in after if after[key] != before.get(key, 0)}
    assert {(method, route) for method, route, _ in changed} <= {("GET", r) for r in READ_ROUTES}
    assert sum(after[k] - before.get(k, 0) for k in changed) == N_REQUESTS


def test_journeys_share_one_trace(shop: Shop) -> None:
    seen: list[tuple[str, str]] = []

    def record(method: str, path: str, payload: Any, headers: Mapping[str, str]) -> tuple[int, Any]:  # noqa: ANN401
        seen.append((path, headers["traceparent"]))
        return 200, {}

    client = ShopSimClient(record)
    flow = requests(CatalogHolder(shop.catalog), client, random.Random(3))

    for task in islice(flow, 50):
        task()

    # Les requêtes d'un même parcours se suivent sous la même trace (une span chacune)
    traces = [parse_traceparent(traceparent) for _, traceparent in seen]
    assert None not in traces
    assert len(set(traces)) < len(traces)
    # Une trace ne revient jamais après un autre parcours
    runs = [trace_id for i, trace_id in enumerate(traces) if i == 0 or traces[i - 1] != trace_id]
    assert len(runs) == len(set(runs))
    track = [path for path, _ in seen]
    assert any(p.startswith("/customers/") and p.endswith("/orders") for p in track)


def test_shipments_only_for_shipped_orders(shop: Shop) -> None:
    rng = random.Random(1)
    for _ in range(300):
        for journey, _weight in visits.JOURNEYS:
            for path in journey(shop.catalog, rng):
                if path.startswith("/shipments/"):
                    order_id = int(path.rsplit("/", 1)[1])
                    assert shop.states[order_id] in {"moving", "delayed", "lost", "delivered"}


# --- Découverte ---------------------------------------------------------------------------


class FakeShop:
    """Clients 1 à 3, deux commandes chacun ; `failing` : chemin qui répond 500."""

    def __init__(self, failing: str | None = None) -> None:
        self.failing = failing

    def __call__(
        self,
        method: str,
        path: str,
        payload: Any,  # noqa: ANN401
        headers: Mapping[str, str],
    ) -> tuple[int, Any]:
        if path == self.failing:
            return 500, {"detail": "Internal Server Error"}
        parts = path.strip("/").split("/")
        customer = int(parts[1])
        if customer > 3:
            return 404, {"detail": "Customer not found"}
        if len(parts) == 2:
            return 200, {"id": customer}
        return 200, [
            {"id": customer * 10 + n, "customer_id": customer, "status": status}
            for n, status in enumerate(["delivered", "pending"])
        ]


def test_discovery_finds_customers_and_orders() -> None:
    catalog = discover(ShopSimClient(FakeShop()))

    assert catalog.customers == [1, 2, 3]
    assert [o.id for o in catalog.orders[2]] == [20, 21]
    assert [o.shipped for o in catalog.orders[2]] == [True, False]


@pytest.mark.parametrize("failing", ["/customers/2", "/customers/3/orders"])
def test_discovery_fails_when_shopsim_answers_badly(failing: str) -> None:
    with pytest.raises(DiscoveryError):
        discover(ShopSimClient(FakeShop(failing)))
