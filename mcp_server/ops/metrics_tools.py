"""Métriques Prometheus par requêtes prédéfinies : jamais de PromQL venu des arguments.

Chaque métrique est une ou plusieurs requêtes écrites ici ; seule la fenêtre (`WINDOWS`, liste
fixe) y est insérée. Aucune métrique de ShopSim ne dit qu'une panne est active (J4.2).
"""

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.ops import common
from mcp_server.ops.sources import PROMETHEUS_QUERY, SourceUnavailable, fetch_json

Window = Literal["1m", "5m", "15m", "1h"]
MAX_SERIES = 20

_P95 = (
    "histogram_quantile(0.95, sum by (le, route) "
    '(rate(http_request_duration_seconds_bucket{job="shopsim"}[{w}]{offset})))'
)
_REQUESTS = 'sum by (route) (rate(http_requests_total{job="shopsim"}[{w}]))'
_ERRORS = 'sum by (route) (rate(http_requests_total{job="shopsim",status=~"5.."}[{w}]))'

# Tendance : la p95 maintenant et il y a 5, 10 et 15 minutes (du plus ancien au plus récent)
TREND_POINTS = {
    "15m_ago": " offset 15m",
    "10m_ago": " offset 10m",
    "5m_ago": " offset 5m",
    "now": "",
}

# Nom → {clé : requête} ; `{w}` reçoit la fenêtre
QUERIES: dict[str, dict[str, str]] = {
    # Part des réponses 5xx par route (0 pour une route sans erreur, jamais absente)
    "shopsim_error_rate": {"value": f"({_ERRORS} or {_REQUESTS} * 0) / {_REQUESTS}"},
    "shopsim_latency_p95": {"value": _P95.replace("{offset}", "")},
    "shopsim_request_rate": {"value": _REQUESTS},
    # Part des collectes réussies sur la fenêtre (1 : toujours joignable)
    "targets_up": {"value": "avg by (job) (avg_over_time(up[{w}]))"},
    "agentpilot_runs": {
        "value": 'sum by (status) (increase(agentpilot_runs_finished_total{agent="support"}[{w}]))'
    },
    # Instantané (la fenêtre ne s'applique pas)
    "agentpilot_state": {
        "loop_state": "agentpilot_loop_state == 1",
        "emergency_stop": "agentpilot_emergency_stop",
        "runs_running": "agentpilot_runs_running",
        "pending_actions": "sum(agentpilot_pending_actions)",
        "budget_spent_ratio": "agentpilot_budget_spent_today_usd / agentpilot_budget_daily_usd",
        "state_up": "agentpilot_state_up",
    },
    "shopsim_latency_trend": {
        key: _P95.replace("{offset}", offset) for key, offset in TREND_POINTS.items()
    },
}
Metric = Literal[
    "shopsim_error_rate",
    "shopsim_latency_p95",
    "shopsim_request_rate",
    "targets_up",
    "agentpilot_runs",
    "agentpilot_state",
    "shopsim_latency_trend",
]


def promql(metric: str, window: str) -> dict[str, str]:
    """Requêtes de la métrique, fenêtre insérée (texte fixe, jamais formaté avec un argument
    libre : `window` vient d'une liste fermée)."""
    return {key: query.replace("{w}", window) for key, query in QUERIES[metric].items()}


async def _vector(query: str) -> list[tuple[dict[str, str], float | None]]:
    try:
        body = await fetch_json(PROMETHEUS_QUERY, {"query": query})
    except SourceUnavailable as exc:
        raise ToolError(str(exc)) from exc
    if body.get("status") != "success":
        raise ToolError(f"Prometheus a refusé la requête : {body.get('error', 'erreur inconnue')}")
    found = []
    for item in body.get("data", {}).get("result", []):
        labels = {k: v for k, v in item.get("metric", {}).items() if k != "__name__"}
        try:
            value = float(item["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            value = None
        found.append((labels, common.rounded(value)))
    return found


def _key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


async def query_metrics(metric: Metric, window: Window = "5m") -> dict[str, Any]:
    """Read Prometheus metrics through a fixed, predefined query (no free-form PromQL).

    Args:
        metric: One of:
            - "shopsim_error_rate": share of 5xx responses per route (0 to 1);
            - "shopsim_latency_p95": 95th percentile response time per route, in seconds;
            - "shopsim_request_rate": requests per second per route;
            - "targets_up": share of successful scrapes per service (job) over the window
              (1 = always reachable, 0 = down the whole window);
            - "agentpilot_runs": Support agent runs finished per status over the window;
            - "agentpilot_state": current loop state, emergency stop, running runs, actions
              waiting for a human, share of the daily budget spent, state readable (1/0)
              (instant values: the window does not apply);
            - "shopsim_latency_trend": p95 per route now and 5, 10 and 15 minutes ago, to tell
              a latency that climbs step by step from one that jumped at once.
        window: Time window of rates and percentiles: "1m", "5m" (default), "15m" or "1h".

    Returns: {"now", "metric", "window", "queries" (the PromQL used), "series": [...]}, at most
    20 series sorted by value (largest first). Most metrics: [{"labels", "value"}].
    "agentpilot_state": [{"labels", <key>: value...}]. "shopsim_latency_trend": [{"route",
    "p95_s": {"15m_ago", "10m_ago", "5m_ago", "now"}, "change_s" (now minus 15 min ago),
    "ratio" (now / 15 min ago)}]. A null value means no traffic at that time, never zero.
    Times are real wall-clock time.

    Errors: Prometheus unreachable, too slow or refusing the query (readable message).
    """
    queries = promql(metric, window)
    rows: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for key, query in queries.items():
        for labels, value in await _vector(query):
            row = rows.setdefault(_key(labels), {"labels": labels})
            row[key] = value
    if metric == "shopsim_latency_trend":
        series = [_trend(row) for row in rows.values()]
        series.sort(key=lambda r: -(r["p95_s"]["now"] or 0))
    else:
        series = list(rows.values())
        series.sort(key=lambda r: -max((v for k, v in r.items() if k != "labels" and v), default=0))
    return {
        "now": common.stamp(common.now()),
        "metric": metric,
        "window": window,
        "queries": queries,
        "series": series[:MAX_SERIES],
        "truncated": len(series) > MAX_SERIES,
    }


def _trend(row: dict[str, Any]) -> dict[str, Any]:
    points: dict[str, float | None] = {key: row.get(key) for key in TREND_POINTS}
    first, last = points["15m_ago"], points["now"]
    change = ratio = None
    if first is not None and last is not None:
        change = common.rounded(last - first)
        ratio = common.rounded(last / first, 2) if first else None
    return {
        "route": row["labels"].get("route"),
        "p95_s": points,
        "change_s": change,
        "ratio": ratio,
    }


METRIC_TOOLS = [query_metrics]
