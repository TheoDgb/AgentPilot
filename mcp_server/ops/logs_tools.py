"""Outils de logs : recherche filtrée et résumé d'une période, sur une liste fixe de services.

Le service se choisit parmi `observability.query.SERVICES` (`shopsim`, `mcp`, `agentpilot`) :
jamais un chemin, jamais `chaos.jsonl`. Le dossier vient de l'environnement (`LOG_DIR`), pas
des arguments. Les docstrings (en anglais) sont les descriptions lues par le modèle.
"""

import math
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_server.ops import common
from observability.query import LEVELS, log_file, recent_lines

LOG_DIR_ENV = "LOG_DIR"
DEFAULT_LOG_DIR = Path("logs")
# Champs déjà rendus autrement (heure → âge) ou sans intérêt pour l'enquête
DROPPED_FIELDS = frozenset({"ts", "service", "logger"})
MAX_ROUTES = 15
MAX_EVENTS = 15

Service = Literal["shopsim", "mcp", "agentpilot"]
Level = Literal["debug", "info", "warning", "error", "critical"]
SinceMinutes = Annotated[int, Field(ge=1, le=1440)]


def log_dir() -> Path:
    return Path(os.environ.get(LOG_DIR_ENV) or DEFAULT_LOG_DIR)


def _window(service: str, since_minutes: int) -> tuple[datetime, list[dict[str, Any]], str | None]:
    """Heure actuelle, lignes de la fenêtre (les plus récentes d'abord) et note éventuelle."""
    reference = common.now()
    try:
        path = log_file(log_dir(), service)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not path.is_file():
        return reference, [], f"Aucun fichier de logs pour {service}."
    since = reference - timedelta(minutes=since_minutes)
    try:
        lines = list(recent_lines(log_dir(), service, since))
    except (OSError, ValueError) as exc:
        raise ToolError(f"Logs de {service} illisibles : {exc}") from exc
    return reference, lines, None


def _status_matches(status: Any, wanted: str) -> bool:  # noqa: ANN401
    if not isinstance(status, int):
        return False
    if wanted.endswith("xx"):
        return status // 100 == int(wanted[0])
    return status == int(wanted)


def compact(line: dict[str, Any], reference: datetime) -> dict[str, Any]:
    """Ligne réduite : âge au lieu de l'heure, textes coupés, traceback réduit à sa fin."""
    out: dict[str, Any] = common.age(datetime.fromisoformat(line["ts"]), reference)
    for key, value in line.items():
        if key in DROPPED_FIELDS or value is None:
            continue
        if key == "traceback" and isinstance(value, str):
            value = next((s for s in reversed(value.splitlines()) if s.strip()), "")
        out[key] = common.short(value)
    return out


async def search_logs(
    service: Service,
    since_minutes: SinceMinutes = 15,
    level: Level | None = None,
    route: Annotated[str | None, Field(pattern=r"^/[^\s]{0,200}$")] = None,
    status: Annotated[str | None, Field(pattern=r"^([1-5][0-9]{2}|[1-5]xx)$")] = None,
    trace_id: Annotated[str | None, Field(pattern=r"^[0-9a-f]{32}$")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Search the JSON logs of one service, newest first.

    Args:
        service: "shopsim" (the shop's HTTP API), "mcp" (tool calls of the Support agent's MCP
            server to ShopSim) or "agentpilot" (agent runs, loop, approvals, received alerts).
        since_minutes: Only lines from the last N minutes (1-1440, default 15).
        level: Minimum level (debug, info, warning, error, critical).
        route: Exact route template, e.g. "/orders/{order_id}" (not a concrete path).
        status: HTTP status, exact ("503") or class ("5xx", "4xx").
        trace_id: 32 hex characters: every line of one agent run or one request chain.
        limit: Maximum number of lines returned (1-50, default 20).

    Returns: {"now", "service", "since_minutes", "matched" (all matching lines in the window),
    "returned", "lines": [{"ago" ("il y a 3 min 12 s"), "age_s", "level", "event",
    "trace_id", and the event fields: route, status, duration_ms, error_type, error...}]}.
    Times are real wall-clock time (not the shop's simulated date): use "ago", never compute
    dates. Long values are cut; a traceback is reduced to its last line.

    Errors: unknown service or invalid filter (refused before reading); unreadable log file.
    A missing log file returns no lines with a "note".
    """
    reference, lines, note = _window(service, since_minutes)
    minimum = LEVELS.index(level) if level is not None else 0
    matched = [
        line
        for line in lines
        if (line.get("level") not in LEVELS or LEVELS.index(line["level"]) >= minimum)
        and (route is None or line.get("route") == route)
        and (status is None or _status_matches(line.get("status"), status))
        and (trace_id is None or line.get("trace_id") == trace_id)
    ]
    kept = [compact(line, reference) for line in matched[:limit]]
    result: dict[str, Any] = {
        "now": common.stamp(reference),
        "service": service,
        "since_minutes": since_minutes,
        "matched": len(matched),
        "returned": len(kept),
        "lines": kept,
    }
    if note:
        result["note"] = note
    return result


def _percentile(values: list[float], fraction: float) -> float:
    """Rang le plus proche, sur une liste triée."""
    index = max(0, min(len(values), math.ceil(fraction * len(values))) - 1)
    return values[index]


async def summarize_logs(service: Service, since_minutes: SinceMinutes = 15) -> dict[str, Any]:
    """Summarize one service's logs over a period: HTTP traffic per route and warnings.

    Args:
        service: "shopsim", "mcp" or "agentpilot" (see search_logs).
        since_minutes: Period: the last N minutes (1-1440, default 15).

    Returns: {"now", "service", "since_minutes", "lines" (lines read),
    "requests": {"total", "errors_5xx", "errors_4xx", "routes": [{"route", "count", "5xx",
    "4xx", "p50_ms", "p95_ms", "last_5xx" ("il y a …" or null)}], "others" (routes beyond the
    first 15: {"routes", "count", "5xx"}) or null},
    "warnings": [{"event", "error_type", "level", "count", "last" ("il y a …")}]}.
    Routes are sorted by 5xx errors, then by volume. Requests come from "http.request" lines
    (shopsim and agentpilot; the mcp service has none). Times are real wall-clock time.

    Errors: unknown service (refused); unreadable log file. A missing log file gives empty
    totals with a "note".
    """
    reference, lines, note = _window(service, since_minutes)
    routes: dict[str, dict[str, Any]] = {}
    warnings: Counter[tuple[str, str, str]] = Counter()
    last_warning: dict[tuple[str, str, str], datetime] = {}
    warning_level = LEVELS.index("warning")
    for line in lines:
        at = datetime.fromisoformat(line["ts"])
        if line.get("event") == "http.request":
            stats = routes.setdefault(
                str(line.get("route") or "unmatched"),
                {"count": 0, "5xx": 0, "4xx": 0, "durations": [], "last_5xx": None},
            )
            stats["count"] += 1
            status = line.get("status")
            if isinstance(status, int) and status >= 500:
                stats["5xx"] += 1
                stats["last_5xx"] = max(stats["last_5xx"] or at, at)
            elif isinstance(status, int) and status >= 400:
                stats["4xx"] += 1
            if isinstance(line.get("duration_ms"), int | float):
                stats["durations"].append(float(line["duration_ms"]))
        level = line.get("level")
        if level in LEVELS and LEVELS.index(level) >= warning_level:
            key = (str(line.get("event")), str(line.get("error_type") or ""), level)
            warnings[key] += 1
            last_warning[key] = max(last_warning.get(key, at), at)

    ordered = sorted(routes.items(), key=lambda item: (-item[1]["5xx"], -item[1]["count"]))
    shown = [
        {
            "route": route,
            "count": stats["count"],
            "5xx": stats["5xx"],
            "4xx": stats["4xx"],
            **_durations(stats["durations"]),
            "last_5xx": (
                common.age(stats["last_5xx"], reference)["ago"] if stats["last_5xx"] else None
            ),
        }
        for route, stats in ordered[:MAX_ROUTES]
    ]
    rest = [stats for _, stats in ordered[MAX_ROUTES:]]
    result: dict[str, Any] = {
        "now": common.stamp(reference),
        "service": service,
        "since_minutes": since_minutes,
        "lines": len(lines),
        "requests": {
            "total": sum(s["count"] for s in routes.values()),
            "errors_5xx": sum(s["5xx"] for s in routes.values()),
            "errors_4xx": sum(s["4xx"] for s in routes.values()),
            "routes": shown,
            "others": (
                {
                    "routes": len(rest),
                    "count": sum(s["count"] for s in rest),
                    "5xx": sum(s["5xx"] for s in rest),
                }
                if rest
                else None
            ),
        },
        "warnings": [
            {
                "event": event,
                "error_type": error_type or None,
                "level": level,
                "count": count,
                "last": common.age(last_warning[(event, error_type, level)], reference)["ago"],
            }
            for (event, error_type, level), count in warnings.most_common(MAX_EVENTS)
        ],
    }
    if note:
        result["note"] = note
    return result


def _durations(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {
        "p50_ms": round(_percentile(ordered, 0.5), 1),
        "p95_ms": round(_percentile(ordered, 0.95), 1),
    }


LOG_TOOLS = [search_logs, summarize_logs]
