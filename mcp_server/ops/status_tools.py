"""Alertes enregistrées par AgentPilot et santé des services, en lecture seule."""

from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_server.ops import common
from mcp_server.ops.sources import (
    AGENTPILOT_ALERTS,
    AGENTPILOT_STATUS,
    ALERTMANAGER_READY,
    ORIGINS,
    PROMETHEUS_READY,
    SHOPSIM_HEALTH,
    Endpoint,
    SourceUnavailable,
    fetch,
    fetch_json,
)

# État de la boucle d'AgentPilot gardé dans la santé (rien de plus de `/control/status`)
AGENTPILOT_FIELDS = ("loop_paused", "pause_reason", "pause_detail", "emergency_stop")


async def list_alerts(
    status: Literal["firing", "resolved"] | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List the alerts received by AgentPilot from Prometheus/Alertmanager, newest first.

    Alerts are symptoms (5xx rate or p95 latency of a route, service down, failing agent
    runs, budget, action waiting too long), never the cause.

    Args:
        status: "firing" (still active) or "resolved"; all if omitted.
        limit: Maximum number of alerts (1-50, default 20).

    Returns: {"now", "count", "alerts": [{"alertname", "status", "severity", "route" (or null),
    "summary", "started" ("il y a …"), "started_age_s", "resolved" ("il y a …" or null),
    "duration" (how long it fired, e.g. "4 min 05 s", or so far if still firing)}]}.
    Times are real wall-clock time (not the shop's simulated date).

    Errors: AgentPilot unreachable, too slow or answering an error (readable message).
    """
    params: dict[str, str | int] = {"limit": limit}
    if status is not None:
        params["status"] = status
    try:
        alerts = await fetch_json(AGENTPILOT_ALERTS, params)
    except SourceUnavailable as exc:
        raise ToolError(str(exc)) from exc
    reference = common.now()
    found = []
    for alert in alerts:
        started = datetime.fromisoformat(alert["starts_at"])
        ended = datetime.fromisoformat(alert["ends_at"]) if alert.get("ends_at") else None
        since = common.age(started, reference)
        lasted = round(((ended or reference) - started).total_seconds())
        found.append(
            {
                "alertname": alert["alertname"],
                "status": alert["status"],
                "severity": alert.get("severity"),
                "route": alert.get("route"),
                "summary": common.short(alert.get("summary")),
                "started": since["ago"],
                "started_age_s": since["age_s"],
                "resolved": common.age(ended, reference)["ago"] if ended else None,
                "duration": common.duration_text(max(0, lasted)),
            }
        )
    return {"now": common.stamp(reference), "count": len(found), "alerts": found}


async def _check(endpoint: Endpoint) -> dict[str, Any]:
    origin = ORIGINS[endpoint.source]
    try:
        reply = await fetch(endpoint)
    except SourceUnavailable as exc:
        return {"service": origin.label, "status": "down", "reason": str(exc)}
    status = "ok" if 200 <= reply.status < 300 else "degraded"
    found: dict[str, Any] = {
        "service": origin.label,
        "status": status,
        "http_status": reply.status,
        "duration_ms": reply.duration_ms,
    }
    if endpoint is AGENTPILOT_STATUS and isinstance(reply.body, dict):
        found["loop"] = {key: reply.body.get(key) for key in AGENTPILOT_FIELDS}
    elif status != "ok":
        found["reason"] = common.short(str(reply.body))
    return found


async def check_health() -> dict[str, Any]:
    """Check whether each service answers: ShopSim, AgentPilot, Prometheus, Alertmanager.

    Returns: {"now", "services": [{"service", "status" ("ok", "degraded": answers with an
    error, "down": unreachable or too slow), "http_status", "duration_ms", "reason" (if not
    ok)}]}. For AgentPilot, "loop" adds its processing loop state: loop_paused, pause_reason,
    pause_detail, emergency_stop.

    Never fails: an unreachable service is reported as "down" with the reason.
    """
    services = [
        await _check(endpoint)
        for endpoint in (SHOPSIM_HEALTH, AGENTPILOT_STATUS, PROMETHEUS_READY, ALERTMANAGER_READY)
    ]
    return {"now": common.stamp(common.now()), "services": services}


STATUS_TOOLS = [list_alerts, check_health]
