"""Métriques HTTP communes : compteur, histogramme, valeurs d'étiquette bornées."""

from observability import metrics
from observability.metrics import HTTP_REGISTRY, exposition


def sample(name: str, labels: dict[str, str]) -> float:
    return HTTP_REGISTRY.get_sample_value(name, labels) or 0.0


def test_observe_feeds_counter_and_histogram() -> None:
    labels = {"method": "GET", "route": "/test-only/{item_id}"}
    before = sample("http_requests_total", labels | {"status": "200"})
    slow = sample("http_request_duration_seconds_bucket", labels | {"le": "5.0"})

    metrics.observe("GET", "/test-only/{item_id}", 200, 4.2)

    assert sample("http_requests_total", labels | {"status": "200"}) == before + 1
    assert sample("http_request_duration_seconds_bucket", labels | {"le": "5.0"}) == slow + 1
    assert sample("http_request_duration_seconds_sum", labels) >= 4.2


def test_unknown_route_and_method_are_grouped() -> None:
    unmatched = {"method": "other", "route": "unmatched", "status": "405"}
    before = sample("http_requests_total", unmatched)

    metrics.observe("BREW", None, 405, 0.001)

    assert sample("http_requests_total", unmatched) == before + 1


def test_exposition_includes_process_and_given_registries() -> None:
    text = exposition(HTTP_REGISTRY).decode()

    assert "# TYPE http_requests_total counter" in text
    assert "python_info" in text
