"""Lecture seule par défaut ; outils d'action seulement avec SHOPSIM_MCP_ACTIONS=1.

Le serveur est lancé en vrai, en stdio, comme le fait Claude Code.
"""

import os
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp_types import Tool

from mcp_server.server import ACTIONS_ENV, build_server

pytestmark = pytest.mark.anyio

READ_TOOLS = {
    "list_open_tickets",
    "get_ticket",
    "get_order",
    "get_customer",
    "list_customer_orders",
    "list_customer_refunds",
    "get_payment",
    "get_shipment",
}
ACTION_TOOLS = {
    "reply_to_ticket",
    "refund_order",
    "resend_order",
    "escalate_ticket",
    "resolve_ticket",
}


async def stdio_tools(actions: str | None) -> list[Tool]:
    env = {"PATH": os.environ.get("PATH", ""), "SHOPSIM_URL": "http://127.0.0.1:1"}
    if actions is not None:
        env[ACTIONS_ENV] = actions
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server.server"], env=env
    )
    async with Client(params) as client:
        return (await client.list_tools()).tools


@pytest.mark.parametrize("actions", [None, "", "0", "true", "yes", " 1"])
async def test_server_is_read_only_without_the_variable(actions: str | None) -> None:
    tools = await stdio_tools(actions)

    assert {tool.name for tool in tools} == READ_TOOLS


async def test_variable_enables_action_tools() -> None:
    tools = {tool.name: tool for tool in await stdio_tools("1")}

    assert set(tools) == READ_TOOLS | ACTION_TOOLS
    for name in ACTION_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.destructive_hint is True
        assert annotations.read_only_hint is False


async def test_action_tools_are_described() -> None:
    async with Client(build_server(enable_actions=True)) as client:
        tools = (await client.list_tools()).tools

    for tool in tools:
        assert tool.description and "Returns" in tool.description and "Errors" in tool.description
        # Aucun paramètre de clé d'idempotence : le serveur la calcule
        assert "idempotency" not in str(tool.input_schema).lower()
