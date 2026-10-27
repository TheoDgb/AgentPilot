"""Métriques Prometheus de ShopSim : requêtes et durées par gabarit de route, rien sur Chaos,
aucune étiquette à forte cardinalité."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from observability.metrics import HTTP_REGISTRY, METHODS, OTHER_METHOD
from shopsim.app.main import app
from shopsim.tests.conftest import JsonLogs, MakePayment

TOKEN = "test-chaos-token-0123"
AUTH = {"X-Chaos-Token": TOKEN}
# Mots qui trahiraient une panne injectée
MARKERS = ("chaos", "simul", "panne", "inject", "fault", "db_errors", "bad_deploy", "saturation")
HTTP_LABELS = {"method", "route", "status", "le"}
ORDER_ROUTE = "/orders/{order_id}"


@pytest.fixture(autouse=True)
def chaos_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", TOKEN)


@pytest.fixture
def no_fault(client: TestClient) -> Iterator[None]:
    yield
    client.delete("/chaos/db_errors", headers=AUTH)


def requests(method: str, route: str, status: int) -> float:
    value = HTTP_REGISTRY.get_sample_value(
        "http_requests_total", {"method": method, "route": route, "status": str(status)}
    )
    return value or 0.0


def observations(method: str, route: str) -> float:
    value = HTTP_REGISTRY.get_sample_value(
        "http_request_duration_seconds_count", {"method": method, "route": route}
    )
    return value or 0.0


def http_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("http_")]


def test_requests_are_counted_by_route_template(
    client: TestClient, make_payment: MakePayment
) -> None:
    orders = [make_payment().order_id for _ in range(3)]
    before = requests("GET", ORDER_ROUTE, 200)
    timed = observations("GET", ORDER_ROUTE)

    for order_id in orders:
        assert client.get(f"/orders/{order_id}").status_code == 200

    # Une seule série pour trois commandes différentes
    assert requests("GET", ORDER_ROUTE, 200) == before + 3
    assert observations("GET", ORDER_ROUTE) == timed + 3
    text = client.get("/metrics").text
    assert not any(f"/orders/{order_id}" in text for order_id in orders)


def test_unknown_paths_share_one_series(client: TestClient) -> None:
    before = requests("GET", "unmatched", 404)

    client.get("/does-not-exist/123")
    client.get("/does-not-exist/456")

    assert requests("GET", "unmatched", 404) == before + 2
    assert "/does-not-exist" not in client.get("/metrics").text


def test_fault_errors_are_counted_like_real_errors(
    client: TestClient, make_payment: MakePayment, no_fault: None
) -> None:
    order_id = make_payment().order_id
    client.post("/chaos/db_errors", headers=AUTH)
    before = requests("GET", ORDER_ROUTE, 500)

    assert client.get(f"/orders/{order_id}").status_code == 500

    assert requests("GET", ORDER_ROUTE, 500) == before + 1


def test_chaos_requests_leave_no_trace_in_metrics(
    client: TestClient, make_payment: MakePayment, no_fault: None
) -> None:
    before = http_lines(client.get("/metrics").text)

    client.get("/chaos", headers=AUTH)
    client.post("/chaos/db_errors", headers=AUTH)
    client.post("/chaos/db_errors", headers=AUTH)
    client.get("/chaos", headers=AUTH)
    client.delete("/chaos/db_errors", headers=AUTH)
    client.post("/chaos/db_errors", headers={"X-Chaos-Token": "mauvais-jeton"})
    client.post("/chaos/unknown", headers=AUTH)
    client.get("/chaos")

    text = client.get("/metrics").text
    assert http_lines(text) == before
    assert not any(marker in text.lower() for marker in MARKERS)


def test_metrics_endpoint_is_neither_counted_nor_logged(
    client: TestClient, json_logs: JsonLogs
) -> None:
    response = client.get("/metrics")
    client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "/metrics" not in response.text
    assert requests("GET", "/metrics", 200) == 0
    assert [line for line in json_logs.read("shopsim") if line.get("path") == "/metrics"] == []


def test_labels_have_bounded_values(client: TestClient, make_payment: MakePayment) -> None:
    for _ in range(3):
        order_id = make_payment().order_id
        client.get(f"/orders/{order_id}")
        client.get(f"/payments/{order_id}")
        client.get(f"/shipments/{order_id}")
    client.get("/customers/999999")
    client.request("BREW", "/orders/1")
    # Gabarits des routes de l'app (FastAPI regroupe les routes incluses : pas de `app.routes`)
    templates = set(app.openapi()["paths"])

    families = text_string_to_metric_families(client.get("/metrics").text)
    samples = [s for f in families if f.name.startswith("http_") for s in f.samples]

    assert samples
    for sample in samples:
        assert set(sample.labels) <= HTTP_LABELS
        route = sample.labels["route"]
        # Registre commun au processus : routes ajoutées par d'autres tests (`/test-only/...`)
        assert route in templates | {"unmatched"} or route.startswith("/test-only/"), route
        assert sample.labels["method"] in METHODS | {OTHER_METHOD}
        assert not sample.labels["route"].startswith("/chaos")
        for name, value in sample.labels.items():
            if name not in {"status", "le"}:
                assert not value.isdigit()
