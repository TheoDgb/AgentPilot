"""Métriques, alertes et santé du serveur Ops : requêtes Prometheus fixes, tendance, erreurs
lisibles, et jamais `/chaos`. Les sources sont simulées par un transport HTTP qui enregistre
chaque requête ; une source injoignable est un vrai port fermé."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, get_args

import httpx
import pytest
from mcp import Client
from mcp_types import CallToolResult

from mcp_server.ops import common, sources
from mcp_server.ops.metrics_tools import QUERIES, TREND_POINTS, Metric, Window, promql
from mcp_server.ops.server import mcp

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 29, 17, 0, 0, tzinfo=UTC)
CLOSED = "http://127.0.0.1:9"  # aucun service n'écoute sur le port 9 (discard)


def vector(*series: tuple[dict[str, str], str]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1761757200, value]} for labels, value in series
            ],
        },
    }


class FakeSources:
    """Répond comme Prometheus, AgentPilot, ShopSim et Alertmanager ; note chaque requête."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.prometheus: dict[str, dict[str, Any]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        port, path = request.url.port, request.url.path
        code = self.status.get(f"{port}{path}", 200)
        if path == "/api/v1/query":
            query = request.url.params["query"]
            body = self.prometheus.get(query, vector())
            return httpx.Response(code, json=body)
        if path == "/alerts":
            return httpx.Response(code, json=self.alerts)
        if path == "/control/status":
            return httpx.Response(
                code,
                json={
                    "loop_paused": True,
                    "pause_reason": "infra",
                    "pause_detail": "config",
                    "emergency_stop": False,
                    "spent_today_usd": "1.2",
                    "paused_by": "x",
                },
            )
        if path == "/health":
            return httpx.Response(code, json={"status": "ok" if code == 200 else "unavailable"})
        if path == "/-/ready":
            return httpx.Response(code, text="Ready.")
        return httpx.Response(404, json={"detail": "Not Found"})

    def queries(self) -> list[str]:
        return [r.url.params["query"] for r in self.requests if r.url.path == "/api/v1/query"]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSources]:
    fake = FakeSources()
    monkeypatch.setattr(sources, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(common, "clock", lambda: NOW)
    for env, port in (
        ("PROMETHEUS_URL", 9090),
        ("AGENTPILOT_URL", 8001),
        ("SHOPSIM_URL", 8000),
        ("ALERTMANAGER_URL", 9093),
    ):
        monkeypatch.setenv(env, f"http://ops-test:{port}")
    yield fake


async def call(name: str, **arguments: Any) -> CallToolResult:  # noqa: ANN401
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments)


def data(result: CallToolResult) -> Any:  # noqa: ANN401
    assert not result.is_error, result.content
    return result.structured_content


def text(result: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


# --- Requêtes Prometheus fixes -------------------------------------------------------------


@pytest.mark.parametrize("metric", get_args(Metric))
@pytest.mark.parametrize("window", get_args(Window))
async def test_every_query_comes_from_the_fixed_table(
    fake: FakeSources, metric: str, window: str
) -> None:
    found = data(await call("query_metrics", metric=metric, window=window))

    expected = promql(metric, window)
    assert fake.queries() == list(expected.values())
    assert found["queries"] == expected
    for query in fake.queries():
        assert f"[{window}]" in query or "[" not in query
        assert "chaos" not in query.lower()


def test_metric_list_matches_the_table() -> None:
    assert set(get_args(Metric)) == set(QUERIES)


@pytest.mark.parametrize(
    "arguments",
    [
        {"metric": "up"},
        {"metric": 'up{job="shopsim"}'},
        {"metric": "shopsim_error_rate", "window": "2h"},
        {"metric": "shopsim_error_rate", "window": "5m] or vector(1) #"},
    ],
)
async def test_unknown_metrics_and_windows_are_refused(
    fake: FakeSources, arguments: dict[str, str]
) -> None:
    result = await call("query_metrics", **arguments)

    assert result.is_error
    assert fake.queries() == []


async def test_an_extra_query_argument_is_never_sent(fake: FakeSources) -> None:
    """Un argument `query` en plus n'est pas un paramètre : seule la requête fixe part."""
    await call("query_metrics", metric="targets_up", query='{__name__=~".+"}')

    assert fake.queries() == list(promql("targets_up", "5m").values())


async def test_series_are_rounded_sorted_and_capped(fake: FakeSources) -> None:
    query = promql("shopsim_error_rate", "5m")["value"]
    fake.prometheus[query] = vector(
        *[({"route": f"/r{n}"}, str(n / 100)) for n in range(25)],
        ({"route": "/nan"}, "NaN"),
    )

    found = data(await call("query_metrics", metric="shopsim_error_rate"))

    assert len(found["series"]) == 20 and found["truncated"]
    assert found["series"][0] == {"labels": {"route": "/r24"}, "value": 0.24}
    assert found["now"] == "2026-10-29T17:00:00Z"


# --- Tendance ----------------------------------------------------------------------------


def trend_answers(fake: FakeSources, points: dict[str, dict[str, str]]) -> None:
    """`points` : route → {point de TREND_POINTS : valeur}."""
    for key, query in promql("shopsim_latency_trend", "5m").items():
        fake.prometheus[query] = vector(
            *[({"route": route}, values[key]) for route, values in points.items() if key in values]
        )


async def test_trend_queries_use_exact_offsets(fake: FakeSources) -> None:
    await call("query_metrics", metric="shopsim_latency_trend")

    queries = fake.queries()
    assert len(queries) == 4
    for key, offset in TREND_POINTS.items():
        query = promql("shopsim_latency_trend", "5m")[key]
        assert query in queries
        assert query.endswith(f"[5m]{offset})))")


async def test_trend_tells_a_climb_from_a_jump(fake: FakeSources) -> None:
    trend_answers(
        fake,
        {
            # Saturation : monte pas à pas
            "/orders/{order_id}": {
                "15m_ago": "0.2",
                "10m_ago": "0.9",
                "5m_ago": "2.1",
                "now": "3.4",
            },
            # Paiements lents : saut d'un coup
            "/payments/{order_id}": {"15m_ago": "0.1", "10m_ago": "0.1", "5m_ago": "4", "now": "4"},
            # Route sans trafic il y a 15 min : point manquant, jamais 0
            "/clock": {"10m_ago": "0.01", "5m_ago": "0.01", "now": "0.02"},
        },
    )

    series = {
        s["route"]: s
        for s in data(await call("query_metrics", metric="shopsim_latency_trend"))["series"]
    }

    climb, jump, new = (
        series["/orders/{order_id}"],
        series["/payments/{order_id}"],
        series["/clock"],
    )
    assert climb["p95_s"] == {"15m_ago": 0.2, "10m_ago": 0.9, "5m_ago": 2.1, "now": 3.4}
    assert (climb["change_s"], climb["ratio"]) == (3.2, 17.0)
    assert jump["p95_s"] == {"15m_ago": 0.1, "10m_ago": 0.1, "5m_ago": 4.0, "now": 4.0}
    assert (jump["change_s"], jump["ratio"]) == (3.9, 40.0)
    assert new["p95_s"]["15m_ago"] is None and new["change_s"] is None and new["ratio"] is None
    # Les plus lentes maintenant d'abord
    assert [
        s["route"]
        for s in data(await call("query_metrics", metric="shopsim_latency_trend"))["series"]
    ][0] == "/payments/{order_id}"


# --- Alertes -----------------------------------------------------------------------------


async def test_alerts_with_ages_and_durations(fake: FakeSources) -> None:
    fake.alerts = [
        {
            "alertname": "ShopSimHighErrorRate",
            "status": "firing",
            "severity": "critical",
            "route": "/orders/{order_id}",
            "summary": "Erreurs 5xx sur /orders/{order_id}",
            "starts_at": "2026-10-29T16:55:30Z",
            "ends_at": None,
        },
        {
            "alertname": "ShopSimHighLatency",
            "status": "resolved",
            "severity": "warning",
            "route": "/payments/{order_id}",
            "summary": "Latence élevée",
            "starts_at": "2026-10-29T15:00:00Z",
            "ends_at": "2026-10-29T15:04:05Z",
        },
    ]

    found = data(await call("list_alerts", status="firing", limit=5))

    [request] = fake.requests
    assert request.url.path == "/alerts" and dict(request.url.params) == {
        "limit": "5",
        "status": "firing",
    }
    firing, resolved = found["alerts"]
    assert (firing["started"], firing["started_age_s"], firing["resolved"]) == (
        "il y a 4 min 30 s",
        270,
        None,
    )
    assert firing["duration"] == "4 min 30 s"
    assert resolved["resolved"] == "il y a 1 h 55 min" and resolved["duration"] == "4 min 05 s"


# --- Santé -------------------------------------------------------------------------------


async def test_health_reports_each_service_without_failing(
    fake: FakeSources, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.status["8000/health"] = 503
    monkeypatch.setenv("ALERTMANAGER_URL", CLOSED)
    monkeypatch.setattr(sources, "transport", _passthrough(fake, closed_port=9))

    found = data(await call("check_health"))

    services = {s["service"]: s for s in found["services"]}
    assert services["ShopSim"]["status"] == "degraded" and services["ShopSim"]["http_status"] == 503
    assert services["AgentPilot"]["loop"] == {
        "loop_paused": True,
        "pause_reason": "infra",
        "pause_detail": "config",
        "emergency_stop": False,
    }
    assert services["Prometheus"]["status"] == "ok"
    assert services["Alertmanager"]["status"] == "down"
    assert "injoignable" in services["Alertmanager"]["reason"]


def _passthrough(fake: FakeSources, closed_port: int) -> httpx.MockTransport:
    """Le faux transport, sauf pour un port : connexion refusée, comme un service arrêté."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == closed_port:
            raise httpx.ConnectError("connexion refusée", request=request)
        return fake(request)

    return httpx.MockTransport(handler)


# --- Erreurs lisibles --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "env", "label"),
    [
        ("query_metrics", {"metric": "targets_up"}, "PROMETHEUS_URL", "Prometheus"),
        ("list_alerts", {}, "AGENTPILOT_URL", "AgentPilot"),
    ],
)
async def test_unreachable_source_gives_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], env: str, label: str
) -> None:
    monkeypatch.setattr(sources, "transport", None)
    monkeypatch.setenv(env, CLOSED)

    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments)
        # Le serveur répond encore après l'erreur
        health = await client.call_tool("check_health", {})

    assert result.is_error and f"{label} est injoignable ({CLOSED})." in text(result)
    assert not health.is_error


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="<html>proxy</html>"), "Réponse illisible de Prometheus"),
        (httpx.Response(503, json={}), "Prometheus a répondu 503"),
        (httpx.Response(200, json={"status": "error", "error": "bad"}), "Prometheus a refusé"),
    ],
)
async def test_bad_prometheus_answer_gives_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response, message: str
) -> None:
    monkeypatch.setattr(sources, "transport", httpx.MockTransport(lambda request: response))

    result = await call("query_metrics", metric="targets_up")

    assert result.is_error and message in text(result)


async def test_slow_source_gives_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("trop lent", request=request)

    monkeypatch.setattr(sources, "transport", httpx.MockTransport(slow))

    result = await call("list_alerts")

    assert result.is_error and "n'a pas répondu en 5 s" in text(result)


# --- Jamais le Chaos ---------------------------------------------------------------------


async def test_no_tool_ever_calls_chaos(fake: FakeSources) -> None:
    for metric in get_args(Metric):
        await call("query_metrics", metric=metric)
    await call("list_alerts")
    await call("list_alerts", status="resolved")
    await call("check_health")

    assert fake.requests
    for request in fake.requests:
        assert "chaos" not in str(request.url).lower()
        assert "x-chaos-token" not in {key.lower() for key in request.headers}
        assert request.method == "GET"
    paths = {request.url.path for request in fake.requests}
    assert paths <= {e.path for e in sources.ENDPOINTS}


def test_endpoints_are_fixed_and_never_chaos() -> None:
    assert {e.path for e in sources.ENDPOINTS} == {
        "/api/v1/query",
        "/-/ready",
        "/alerts",
        "/control/status",
        "/health",
    }
