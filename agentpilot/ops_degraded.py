"""Rapport sans IA sur une alerte (J6.5) : quand l'agent Ops ne peut pas enquêter.

Quand : budget du jour insuffisant pour une enquête, IA connue indisponible (boucle en pause
sur une erreur d'infrastructure), plafond horaire des enquêtes automatiques atteint, ou enquête
par l'IA arrêtée sur une erreur d'infrastructure.

Comment : du code seul, sur les **mêmes outils** que l'agent. Le service lance le serveur MCP
Ops avec la même configuration que l'agent (`ops_agent.mcp_server_config` : environnement
construit champ par champ, jamais `CHAOS_TOKEN`) et appelle ses outils par le client MCP, dans
un ordre fixe. Chaque appel est inscrit au journal (`tool_calls`, auteur `no_ai`), comme ceux
d'une enquête.

Le rapport (`DegradedReport`) ne donne que des faits : santé des services, routes en erreur et
types d'erreur, routes lentes et tendance, alertes actives, étendue. Jamais de cause : une règle
qui classerait les pannes ferait passer une heuristique pour un diagnostic. Une source
injoignable n'arrête pas le rapport : elle y figure, avec le motif.
"""

import asyncio
import itertools
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from agentpilot import ops_agent
from agentpilot.hooks import OPS_MAX_RESPONSE_CHARS, clip
from agentpilot.journal import CallEntry, Journal
from agentpilot.models import CallOutcome
from agentpilot.ops_agent import AlertBrief
from agentpilot.ops_schemas import (
    AlertFact,
    DegradedReason,
    DegradedReport,
    ErrorTypeFact,
    RouteErrorFact,
    ServiceFact,
    SlowRouteFact,
)
from observability import logs, trace
from observability.logs import log_event

logger = logging.getLogger(__name__)

# Auteur des appels au journal : la plateforme, sans IA
AUTHOR = "no_ai"
SINCE_MINUTES = 15
# Mêmes seuils que les règles d'alerte (`monitoring/prometheus/rules/alerts.yml`)
ERROR_SHARE = 0.05
SLOW_MS = 1_000.0
# Routes qui ne disent rien d'une panne métier
IGNORED_ROUTES = frozenset({"/health", "/clock", "/metrics", "unmatched"})
# Outils appelés, dans cet ordre (nom court, arguments)
CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("check_health", {}),
    ("list_alerts", {"status": "firing"}),
    ("summarize_logs", {"service": "shopsim", "since_minutes": SINCE_MINUTES}),
    ("query_metrics", {"metric": "shopsim_error_rate", "window": "5m"}),
    ("query_metrics", {"metric": "shopsim_latency_trend"}),
)


class ToolFailed(Exception):
    """L'outil a répondu par une erreur (source injoignable…) : message lisible."""


# Appelle un outil Ops par son nom court ; renvoie sa réponse décodée, lève `ToolFailed`
Caller = Callable[[str, dict[str, Any]], Awaitable[Any]]
Connect = Callable[[], AbstractAsyncContextManager[Caller]]


@asynccontextmanager
async def mcp_tools(environ: Mapping[str, str] = os.environ) -> Any:  # noqa: ANN401
    """Serveur MCP Ops lancé comme pour l'agent, et appel de ses outils par le client MCP."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = ops_agent.mcp_server_config(
        environ, trace_id=trace.current_trace_id(), log_dir=logs.log_dir()
    )
    server = StdioServerParameters(
        command=config["command"], args=config["args"], env=config["env"]
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
            result = await session.call_tool(name, arguments)
            text = "".join(getattr(block, "text", "") for block in result.content)
            if result.is_error:
                raise ToolFailed(text or f"{name} en erreur")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

        yield call


async def collect(caller: Caller, journal: Journal) -> tuple[dict[tuple[str, str], Any], list[str]]:
    """Appelle les outils dans l'ordre fixe et inscrit chaque appel au journal. Renvoie les
    réponses (par outil et première valeur d'argument) et les sources injoignables."""
    seq = itertools.count(1)
    replies: dict[tuple[str, str], Any] = {}
    unavailable: list[str] = []
    for name, arguments in CALLS:
        key = (name, str(next(iter(arguments.values()), "")))
        start = time.perf_counter()
        try:
            reply = await caller(name, arguments)
        except Exception as exc:  # noqa: BLE001  (source injoignable : notée, le rapport continue)
            message = str(exc) or type(exc).__name__
            unavailable.append(f"{name} : {message}")
            outcome, response, error = CallOutcome.FAILED, None, message
        else:
            replies[key] = reply
            outcome, response, error = CallOutcome.SUCCEEDED, reply, None
        await asyncio.to_thread(
            journal.record_call,
            CallEntry(
                seq=next(seq),
                tool_use_id=None,
                tool=f"mcp__{ops_agent.MCP_SERVER}__{name}",
                input=arguments,
                outcome=outcome,
                response=None if response is None else clip(response, OPS_MAX_RESPONSE_CHARS),
                error=error,
                duration_ms=round((time.perf_counter() - start) * 1000),
                agent=AUTHOR,
            ),
        )
    return replies, unavailable


def _routes(summary: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    if not isinstance(summary, dict):
        return []
    routes = (summary.get("requests") or {}).get("routes") or []
    return [r for r in routes if isinstance(r, dict) and r.get("route") not in IGNORED_ROUTES]


def build(
    alert: AlertBrief,
    reason: DegradedReason,
    replies: Mapping[tuple[str, str], Any],
    unavailable: list[str],
    now: datetime,
    reason_detail: str | None = None,
) -> DegradedReport:
    """Faits relevés dans les réponses des outils ; jamais de cause."""
    health = replies.get(("check_health", ""), {})
    services = [
        ServiceFact(service=s["service"], status=s["status"], detail=s.get("reason"))
        for s in (health.get("services") or [] if isinstance(health, dict) else [])
    ]
    summary = replies.get(("summarize_logs", "shopsim"))
    routes = _routes(summary)
    rates = {
        s["labels"].get("route"): s.get("value")
        for s in (replies.get(("query_metrics", "shopsim_error_rate")) or {}).get("series", [])
        if isinstance(s, dict) and isinstance(s.get("labels"), dict)
    }
    error_routes = [
        RouteErrorFact(
            route=r["route"],
            requests=r["count"],
            errors_5xx=r["5xx"],
            share=round(r["5xx"] / r["count"], 4),
            error_rate_5m=rates.get(r["route"]),
        )
        for r in routes
        if r.get("count") and r.get("5xx") and r["5xx"] / r["count"] > ERROR_SHARE
    ]
    warnings = summary.get("warnings") or [] if isinstance(summary, dict) else []
    error_types = [
        ErrorTypeFact(error_type=w["error_type"], count=w["count"], last=w.get("last"))
        for w in warnings
        if w.get("event") == "http.exception" and w.get("error_type")
    ]
    trend = {
        s.get("route"): s.get("p95_s") or {}
        for s in (replies.get(("query_metrics", "shopsim_latency_trend")) or {}).get("series", [])
        if isinstance(s, dict)
    }
    slow = {r["route"]: r.get("p95_ms") for r in routes if (r.get("p95_ms") or 0) > SLOW_MS}
    slow |= {
        route: slow.get(route)
        for route, points in trend.items()
        if route not in IGNORED_ROUTES and (points.get("now") or 0) * 1000 > SLOW_MS
    }
    slow_routes = [
        SlowRouteFact(
            route=route,
            p95_ms=p95_ms,
            p95_now_s=trend.get(route, {}).get("now"),
            p95_15m_ago_s=trend.get(route, {}).get("15m_ago"),
        )
        for route, p95_ms in sorted(slow.items())
    ]
    firing = replies.get(("list_alerts", "firing"))
    active = [
        AlertFact(alertname=a["alertname"], route=a.get("route"), started=a.get("started"))
        for a in (firing.get("alerts") or [] if isinstance(firing, dict) else [])
    ]
    affected = sorted({r.route for r in error_routes} | {r.route for r in slow_routes})
    return DegradedReport(
        reason=reason,
        reason_detail=reason_detail,
        alert_id=str(alert.id),
        alertname=alert.alertname,
        alert_route=alert.route,
        generated_at=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        since_minutes=SINCE_MINUTES,
        health=services,
        error_routes=error_routes,
        error_types=error_types,
        slow_routes=slow_routes,
        active_alerts=active,
        affected_routes=affected,
        observed_routes=len(routes),
        unavailable=unavailable,
    )


async def degraded_report(
    alert: AlertBrief,
    reason: DegradedReason,
    *,
    journal: Journal,
    connect: Connect = mcp_tools,
    reason_detail: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DegradedReport:
    """Rapport sans IA sur l'alerte. Le serveur MCP qui ne démarre pas du tout : rapport vide,
    avec le motif dans `unavailable`."""
    try:
        async with connect() as caller:
            replies, unavailable = await collect(caller, journal)
    except Exception as exc:  # noqa: BLE001  (aucune source : le rapport le dit)
        replies, unavailable = {}, [f"serveur MCP Ops : {exc!r}"]
    report = build(alert, reason, replies, unavailable, now(), reason_detail)
    log_event(
        logger,
        "ops.degraded_report",
        logging.ERROR,
        alertname=alert.alertname,
        reason=reason,
        **({"error_type": reason_detail} if reason_detail else {}),
    )
    return report
