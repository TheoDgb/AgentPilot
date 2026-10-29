"""Outils de logs du serveur Ops : liste fixe de services, `chaos.jsonl` hors d'atteinte, âges
déjà calculés, réponses compactes. Logs inventés dans un dossier temporaire, horloge figée."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp_types import CallToolResult

from mcp_server.ops import common
from mcp_server.ops.server import mcp

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 29, 17, 0, 0, tzinfo=UTC)
MARK = "CHAOS-MARK-7f3a"
TRACE = "a" * 32
# Noms de service piégés : chemins, autre fichier, casse, octet nul, vide
TRAPS = [
    "chaos",
    "chaos.jsonl",
    "../chaos",
    "shopsim/../chaos",
    "/tmp/x",
    "SHOPSIM",
    "shopsim\x00",
    "shopsim.jsonl",
    "mcp/../../etc/passwd",
    "ops_mcp",
    "",
]


def line(minutes_ago: float, event: str = "http.request", **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    ts = NOW - timedelta(minutes=minutes_ago)
    return {
        "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "service": "shopsim",
        "level": fields.pop("level", "info"),
        "event": event,
        "trace_id": fields.pop("trace_id", "b" * 32),
        "logger": "observability.http",
        **fields,
    }


def write(path: Path, lines: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write(
        tmp_path / "shopsim.jsonl",
        [
            line(120, route="/orders/{order_id}", status=200, duration_ms=12),  # hors fenêtre
            line(10, route="/orders/{order_id}", status=200, duration_ms=10),
            line(8, route="/orders/{order_id}", status=500, duration_ms=30, trace_id=TRACE),
            line(
                8,
                "http.exception",
                level="error",
                route="/orders/{order_id}",
                error_type="OperationalError",
                error="x" * 500,
                traceback="Traceback (most recent call last):\n  File ...\nOperationalError: boom",
                trace_id=TRACE,
            ),
            line(5, route="/payments/{order_id}", status=200, duration_ms=4100),
            line(3, route="/orders/{order_id}", status=404, duration_ms=8),
            line(1, route="/orders/{order_id}", status=503, duration_ms=25),
        ],
    )
    write(tmp_path / "chaos.jsonl", [line(1, "chaos.request", note=MARK)])
    (tmp_path / "ops_mcp.jsonl").write_text(json.dumps(line(1, note=MARK)) + "\n")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(common, "clock", lambda: NOW)
    return tmp_path


async def call(name: str, **arguments: Any) -> CallToolResult:  # noqa: ANN401
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments)


def data(result: CallToolResult) -> Any:  # noqa: ANN401
    assert not result.is_error, result.content
    return result.structured_content


def text(result: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content) + json.dumps(
        result.structured_content or {}
    )


@pytest.mark.parametrize("tool", ["search_logs", "summarize_logs"])
@pytest.mark.parametrize("service", TRAPS)
async def test_trapped_service_names_are_refused(logs: Path, tool: str, service: str) -> None:
    result = await call(tool, service=service)

    assert result.is_error
    assert MARK not in text(result)


async def test_chaos_log_never_appears_in_any_answer(logs: Path) -> None:
    for service in ("shopsim", "mcp", "agentpilot"):
        for result in (
            await call("search_logs", service=service, since_minutes=1440, limit=50),
            await call("summarize_logs", service=service, since_minutes=1440),
        ):
            assert not result.is_error
            assert MARK not in text(result) and "chaos" not in text(result)


async def test_symbolic_link_to_the_chaos_log_is_refused(logs: Path) -> None:
    (logs / "mcp.jsonl").symlink_to(logs / "chaos.jsonl")

    result = await call("search_logs", service="mcp", since_minutes=1440)

    assert result.is_error and "lien symbolique" in text(result)
    assert MARK not in text(result)


async def test_search_gives_ages_newest_first_and_compact_lines(logs: Path) -> None:
    found = data(await call("search_logs", service="shopsim", since_minutes=15))

    assert found["now"] == "2026-10-29T17:00:00Z"
    assert found["matched"] == found["returned"] == 6  # la ligne d'il y a 2 h est hors fenêtre
    first = found["lines"][0]
    assert (first["ago"], first["age_s"], first["status"]) == ("il y a 1 min 00 s", 60, 503)
    assert "ts" not in first and "logger" not in first and "service" not in first
    error = next(item for item in found["lines"] if item["event"] == "http.exception")
    assert error["traceback"] == "OperationalError: boom"
    assert len(error["error"]) == common.MAX_TEXT and error["error"].endswith("…")


@pytest.mark.parametrize(
    ("filters", "statuses"),
    [
        ({"status": "5xx"}, [503, 500]),
        ({"status": "404"}, [404]),
        ({"route": "/payments/{order_id}"}, [200]),
        ({"trace_id": TRACE}, [None, 500]),
        ({"level": "error"}, [None]),
        ({"since_minutes": 2}, [503]),
        ({"limit": 2}, [503, 404]),
    ],
)
async def test_search_filters(logs: Path, filters: dict[str, Any], statuses: list[Any]) -> None:
    found = data(await call("search_logs", service="shopsim", **filters))

    assert [item.get("status") for item in found["lines"]] == statuses


@pytest.mark.parametrize(
    "filters",
    [
        {"status": "6xx"},
        {"status": "5XX"},
        {"trace_id": "not-a-trace"},
        {"route": "orders"},
        {"limit": 51},
        {"since_minutes": 0},
        {"since_minutes": 1441},
        {"level": "fatal"},
    ],
)
async def test_invalid_filters_are_refused(logs: Path, filters: dict[str, Any]) -> None:
    assert (await call("search_logs", service="shopsim", **filters)).is_error


async def test_summary_per_route_and_warnings(logs: Path) -> None:
    summary = data(await call("summarize_logs", service="shopsim", since_minutes=15))

    requests = summary["requests"]
    assert (requests["total"], requests["errors_5xx"], requests["errors_4xx"]) == (5, 2, 1)
    orders, payments = requests["routes"]
    assert orders == {
        "route": "/orders/{order_id}",
        "count": 4,
        "5xx": 2,
        "4xx": 1,
        "p50_ms": 10.0,
        "p95_ms": 30.0,
        "last_5xx": "il y a 1 min 00 s",
    }
    assert payments["route"] == "/payments/{order_id}" and payments["p95_ms"] == 4100.0
    assert requests["others"] is None
    assert summary["warnings"] == [
        {
            "event": "http.exception",
            "error_type": "OperationalError",
            "level": "error",
            "count": 1,
            "last": "il y a 8 min 00 s",
        }
    ]


async def test_summary_keeps_only_fifteen_routes(logs: Path) -> None:
    write(
        logs / "agentpilot.jsonl",
        [line(1, route=f"/r{n}", status=500 if n < 3 else 200) for n in range(20)],
    )

    requests = data(await call("summarize_logs", service="agentpilot"))["requests"]

    assert len(requests["routes"]) == 15
    # Routes en erreur d'abord (à égalité, dans l'ordre de lecture)
    assert {r["route"] for r in requests["routes"][:3]} == {"/r0", "/r1", "/r2"}
    assert requests["others"] == {"routes": 5, "count": 5, "5xx": 0}


async def test_missing_log_file_gives_a_note(logs: Path) -> None:
    found = data(await call("search_logs", service="agentpilot"))

    assert found["lines"] == [] and "Aucun fichier" in found["note"]
