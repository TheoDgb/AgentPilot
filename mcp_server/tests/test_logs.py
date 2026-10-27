"""Logs JSON du serveur MCP : trace reçue et transmise à ShopSim, outils journalisés sans texte."""

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.server import build_server
from observability import trace
from observability.logs import configure
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.models import MessageAuthor
from shopsim.tests.conftest import JsonLogs, MakeCustomer, MakeTicket

pytestmark = pytest.mark.anyio

TRACE = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture
def mcp_logs(json_logs: JsonLogs) -> Iterator[JsonLogs]:
    """Le serveur MCP (dans ce processus) écrit dans `mcp.jsonl`, ShopSim dans `shopsim.jsonl`."""
    configure("mcp", json_logs.dir, logger_name="mcp_server", stream=io.StringIO())
    yield json_logs


def events(logs: JsonLogs, name: str, event: str) -> list[dict]:
    return [line for line in logs.read(name) if line["event"] == event]


async def test_tool_call_carries_the_trace_to_shopsim(
    shopsim: str, mcp_logs: JsonLogs, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer())

    with trace.bind(trace_id=TRACE):
        await build_server(enable_actions=False).call_tool("get_ticket", {"ticket_id": ticket.id})

    (tool,) = events(mcp_logs, "mcp", "tool.call")
    assert tool["service"] == "mcp"
    assert (tool["tool"], tool["ticket_id"], tool["outcome"]) == ("get_ticket", ticket.id, "ok")
    assert tool["trace_id"] == TRACE
    (call,) = events(mcp_logs, "mcp", "shopsim.call")
    assert (call["method"], call["path"], call["status"]) == ("GET", f"/tickets/{ticket.id}", 200)
    assert call["trace_id"] == TRACE
    (request,) = events(mcp_logs, "shopsim", "http.request")
    assert request["route"] == "/tickets/{ticket_id}"
    assert request["trace_id"] == TRACE


async def test_failed_tool_call_is_logged(shopsim: str, mcp_logs: JsonLogs) -> None:
    with trace.bind(trace_id=TRACE), pytest.raises(ToolError):
        await build_server(enable_actions=False).call_tool("get_order", {"order_id": 999_999_999})

    (tool,) = events(mcp_logs, "mcp", "tool.call")
    assert (tool["outcome"], tool["level"], tool["order_id"]) == ("error", "warning", 999_999_999)
    assert tool["error_type"].endswith("ToolError")
    (call,) = events(mcp_logs, "mcp", "shopsim.call")
    assert (call["status"], call["outcome"]) == (404, "not_found")


async def test_reply_text_and_reasons_are_never_logged(
    shopsim: str, mcp_logs: JsonLogs, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    customer_text = "Je suis joignable au 06 98 76 54 32"
    ticket = make_ticket(
        make_customer(), messages=[(MessageAuthor.CUSTOMER, customer_text, SIMULATION_NOW)]
    )
    server = build_server(enable_actions=True)
    reply = "Bonjour Monsieur Durand, nous vous rappelons au 06 98 76 54 32."

    with trace.bind(trace_id=TRACE):
        await server.call_tool("get_ticket", {"ticket_id": ticket.id})
        await server.call_tool("reply_to_ticket", {"ticket_id": ticket.id, "body": reply})
        await server.call_tool(
            "escalate_ticket", {"ticket_id": ticket.id, "reason": "Client menace de porter plainte"}
        )

    tools = [line["tool"] for line in events(mcp_logs, "mcp", "tool.call")]
    assert tools == ["get_ticket", "reply_to_ticket", "escalate_ticket"]
    text = mcp_logs.everything()
    for private in ("Durand", "06 98 76 54 32", "porter plainte", "Bonjour"):
        assert private not in text


async def stdio_call(env: dict[str, str], ticket_id: int) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env={"PATH": os.environ.get("PATH", "")} | env,
    )
    async with Client(params) as client:
        await client.call_tool("get_ticket", {"ticket_id": ticket_id})


async def test_real_server_takes_its_trace_from_the_environment(
    shopsim: str, json_logs: JsonLogs, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer())
    env = {
        "SHOPSIM_URL": shopsim,
        "LOG_DIR": str(json_logs.dir),
        trace.TRACEPARENT_ENV: trace.traceparent(TRACE),
    }

    await stdio_call(env, ticket.id)

    lines = json_logs.read("mcp")
    assert [line["event"] for line in lines] == ["mcp.started", "shopsim.call", "tool.call"]
    assert {line["trace_id"] for line in lines} == {TRACE}
    assert lines[0]["mode"] == "read_only"
    (request,) = events(json_logs, "shopsim", "http.request")
    assert request["trace_id"] == TRACE


async def test_real_server_without_trace_creates_its_own(
    shopsim: str, json_logs: JsonLogs, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    ticket = make_ticket(make_customer())

    await stdio_call(
        {"SHOPSIM_URL": shopsim, "LOG_DIR": str(json_logs.dir), "TRACEPARENT": "invalide"},
        ticket.id,
    )

    (trace_id,) = {line["trace_id"] for line in json_logs.read("mcp")}
    assert trace_id is not None and len(trace_id) == 32
    (request,) = events(json_logs, "shopsim", "http.request")
    assert request["trace_id"] == trace_id


def test_real_server_writes_only_json_on_stderr() -> None:
    """Le SDK MCP pose un gestionnaire texte à l'import : `main` le retire."""
    done = subprocess.run(
        [sys.executable, "-m", "mcp_server.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "SHOPSIM_URL": "http://127.0.0.1:1"},
    )

    lines = done.stderr.splitlines()
    assert lines, done.stderr
    assert json.loads(lines[0])["event"] == "mcp.started"
    for line in lines:
        json.loads(line)
