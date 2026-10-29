"""Serveur MCP Ops (J6.1) : liste exacte des outils, lecture seule, séparé du serveur Support.

Le serveur est lancé en vrai, en stdio, comme le fera AgentPilot.
"""

import os
import subprocess
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp_types import Tool

from mcp_server.tests.test_server_modes import ACTION_TOOLS, READ_TOOLS

pytestmark = pytest.mark.anyio

OPS_TOOLS = {"search_logs", "summarize_logs", "query_metrics", "list_alerts", "check_health"}
# Aucun paramètre qui laisserait choisir un fichier, une requête ou une adresse
FORBIDDEN_PARAMETERS = {"path", "file", "filename", "dir", "query", "promql", "url", "endpoint"}


async def stdio_tools() -> list[Tool]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        # Même avec la variable du serveur Support : rien d'autre que les outils Ops
        "SHOPSIM_MCP_ACTIONS": "1",
        "PROMETHEUS_URL": "http://127.0.0.1:9",
        "AGENTPILOT_URL": "http://127.0.0.1:9",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server.ops.server"], env=env
    )
    async with Client(params) as client:
        return (await client.list_tools()).tools


async def test_exact_list_of_read_only_tools() -> None:
    tools = await stdio_tools()

    assert {tool.name for tool in tools} == OPS_TOOLS
    assert not OPS_TOOLS & (READ_TOOLS | ACTION_TOOLS)
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint, tool.name
        assert not tool.annotations.destructive_hint
        assert tool.description and "Returns" in tool.description, tool.name
        parameters = set((tool.input_schema.get("properties") or {}).keys())
        assert not parameters & FORBIDDEN_PARAMETERS, tool.name
        assert "chaos" not in tool.name and "chaos" not in tool.description.lower()


def test_ops_server_imports_no_support_tool() -> None:
    """Aucun module d'outils de l'agent Support n'est chargé par le serveur Ops."""
    code = (
        "import sys, mcp_server.ops.server; "
        "print(sorted(m for m in sys.modules if m.startswith('mcp_server')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    for module in ("mcp_server.read_tools", "mcp_server.action_tools", "mcp_server.server"):
        assert f"'{module}'" not in loaded
    assert "mcp_server.shopsim_client" not in loaded
