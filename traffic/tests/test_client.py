"""Liste blanche du client : jamais Chaos, jamais une action, rien n'est envoyé hors liste."""

from collections.abc import Mapping
from typing import Any

import pytest

from observability.trace import parse_traceparent
from traffic.client import ForbiddenRequest, ShopSimClient, http_sender, route_of

FORBIDDEN = [
    ("GET", "/chaos"),
    ("POST", "/chaos/db_errors"),
    ("DELETE", "/chaos/db_errors"),
    ("POST", "/refunds"),
    ("POST", "/orders/12/resend"),
    ("POST", "/tickets/3/reply"),
    ("POST", "/tickets/3/escalate"),
    ("POST", "/tickets/3/resolve"),
    ("GET", "/metrics"),
    ("GET", "/customers/1/refunds"),
    ("DELETE", "/orders/12"),
    ("PUT", "/tickets"),
    ("GET", "/orders/12/../../chaos"),
    ("GET", "/orders/abc"),
]


class Recorder:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.sent: list[tuple[str, str, Any, dict[str, str]]] = []

    def __call__(
        self,
        method: str,
        path: str,
        payload: Any,  # noqa: ANN401
        headers: Mapping[str, str],
    ) -> tuple[int, Any]:
        self.sent.append((method, path, payload, dict(headers)))
        return self.status, {}


@pytest.mark.parametrize(("method", "path"), FORBIDDEN)
def test_forbidden_requests_are_never_sent(method: str, path: str) -> None:
    recorder = Recorder()
    client = ShopSimClient(recorder)

    with pytest.raises(ForbiddenRequest):
        client.request(method, path)

    assert recorder.sent == []


@pytest.mark.parametrize(
    ("method", "path", "route"),
    [
        ("GET", "/customers/7", "/customers/{customer_id}"),
        ("GET", "/customers/7/orders", "/customers/{customer_id}/orders"),
        ("GET", "/orders/12", "/orders/{order_id}"),
        ("GET", "/payments/12", "/payments/{order_id}"),
        ("GET", "/shipments/12", "/shipments/{order_id}"),
        ("GET", "/clock", "/clock"),
        ("GET", "/tickets?status=open&limit=100&offset=0", "/tickets"),
        ("POST", "/tickets", "/tickets"),
    ],
)
def test_allowed_routes(method: str, path: str, route: str) -> None:
    assert route_of(method, path) == route


def test_requests_carry_a_trace_and_are_counted() -> None:
    recorder = Recorder(status=404)
    client = ShopSimClient(recorder)

    client.get("/orders/12", "4bf92f3577b34da6a3ce929d0e0e4736")
    client.get("/orders/13")

    (_, _, _, traced), (_, _, _, untraced) = recorder.sent
    assert parse_traceparent(traced["traceparent"]) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "traceparent" not in untraced
    assert traced["User-Agent"] == "shopsim-traffic"
    counts, dropped, created = client.stats.take()
    assert counts == {"GET /orders/{order_id} 404": 2}
    assert (dropped, created) == (0, 0)
    assert client.stats.take() == ({}, 0, 0)


def test_unreachable_shopsim_is_counted_as_an_error() -> None:
    client = ShopSimClient(http_sender("http://127.0.0.1:9", timeout_s=2))

    assert client.get("/clock") == (0, None)
    assert client.stats.take()[0] == {"GET /clock error": 1}
