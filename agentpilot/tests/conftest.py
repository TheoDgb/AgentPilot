"""Fixtures d'AgentPilot : faux Claude Agent SDK, journal en mémoire, base de test (schéma
`agentpilot`) et ShopSim qui tourne vraiment (fixtures des tests du serveur MCP)."""

import itertools
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import DropSchema

from agentpilot import support_agent
from agentpilot.db import SCHEMA, init_db
from agentpilot.journal import CallEntry, PendingEntry
from agentpilot.support_agent import SUPPORT_TOOL_NAMES
from mcp_server.action_tools import ACTION_TOOLS
from mcp_server.read_tools import READ_TOOLS
from mcp_server.tests import conftest as mcp_fixtures

# Fixtures du serveur MCP, réexportées : ShopSim servi en HTTP sur la base de test
db_engine = mcp_fixtures.db_engine
session = mcp_fixtures.session
no_payment_latency = mcp_fixtures.no_payment_latency
make_customer = mcp_fixtures.make_customer
make_order = mcp_fixtures.make_order
make_payment = mcp_fixtures.make_payment
make_ticket = mcp_fixtures.make_ticket
make_shipment = mcp_fixtures.make_shipment
make_refund = mcp_fixtures.make_refund
sim_clock = mcp_fixtures.sim_clock
shopsim_server = mcp_fixtures.shopsim_server
shopsim = mcp_fixtures.shopsim

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim_test"
)
# Date de l'horloge de ShopSim (GET /clock), remplacée dans les tests
TODAY = date(2026, 7, 6)
MODEL = "claude-sonnet-5-5"
# Outils attendus dans le service : 8 de lecture, 5 d'action et la sortie structurée
INIT_TOOLS = ["StructuredOutput", *sorted(SUPPORT_TOOL_NAMES)]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_network_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pas d'appel à ShopSim : l'horloge renvoie la date du seed."""
    monkeypatch.setattr(support_agent, "simulation_today", lambda url: TODAY)


# --- Faux Claude Agent SDK -------------------------------------------------------------


def init_message(tools: list[str] | None = None, status: str = "connected") -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": MODEL,
            "tools": INIT_TOOLS if tools is None else tools,
            "mcp_servers": [{"name": "shopsim", "status": status}],
        },
    )


def tool_use(tool_id: str, name: str, **arguments: Any) -> AssistantMessage:  # noqa: ANN401
    return AssistantMessage(
        content=[
            TextBlock(text="Je regarde."),
            ToolUseBlock(id=tool_id, name=name, input=arguments),
        ],
        model=MODEL,
    )


def tool_result(tool_id: str, is_error: bool = False) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_id, content="{}", is_error=is_error)]
    )


def result_message(output: Any = None, **changes: Any) -> ResultMessage:  # noqa: ANN401
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 8_000,
        "duration_api_ms": 7_000,
        "is_error": False,
        "num_turns": 4,
        "session_id": "session-test",
        "total_cost_usd": 0.0217,
        "structured_output": output,
    }
    return ResultMessage(**(fields | changes))


class FakeQuery:
    """Remplace `claude_agent_sdk.query` : note les options reçues, rejoue des messages."""

    def __init__(self, *messages: Any, error: Exception | None = None) -> None:  # noqa: ANN401
        self.messages = messages
        self.error = error
        self.prompt: str | None = None
        self.options: ClaudeAgentOptions | None = None

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompt = prompt
        self.options = options
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error


Executor = Callable[[str, dict[str, Any]], Awaitable[Any]]
_MCP_TOOLS = {tool.__name__: tool for tool in [*READ_TOOLS, *ACTION_TOOLS]}
_call_ids = itertools.count(1)


async def run_mcp_tool(name: str, arguments: dict[str, Any]) -> Any:  # noqa: ANN401
    """Exécute le vrai outil du serveur MCP (contre la ShopSim de test)."""
    return await _MCP_TOOLS[name.rsplit("__", 1)[-1]](**arguments)


@dataclass
class Call:
    """Appel d'outil scénarisé ; après passage, ce que l'agent en a reçu."""

    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: f"toolu_{next(_call_ids)}")
    denied: str | None = None
    response: Any = None
    error: str | None = None


def call(name: str, **arguments: Any) -> Call:  # noqa: ANN401
    return Call(f"mcp__shopsim__{name}", arguments)


class HookDrivingQuery:
    """Faux SDK qui fait comme le CLI : avant chaque outil, les hooks `PreToolUse` des
    options ; si aucun ne refuse, l'outil est exécuté, puis `PostToolUse` (ou
    `PostToolUseFailure`)."""

    def __init__(
        self,
        *calls: Call,
        output: Any = None,  # noqa: ANN401
        executor: Executor = run_mcp_tool,
    ) -> None:
        self.calls = calls
        self.output = output
        self.executor = executor
        self.options: ClaudeAgentOptions | None = None

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.options = options
        return self._stream()

    async def _hooks(self, event: str, data: dict[str, Any], tool_use_id: str) -> list[Any]:
        assert self.options is not None and self.options.hooks is not None
        outputs = []
        for matcher in self.options.hooks.get(event, []):  # type: ignore[call-overload]
            for hook in matcher.hooks:
                outputs.append(await hook(data, tool_use_id, {"signal": None}))
        return outputs

    async def _run(self, step: Call) -> None:
        base = {"tool_name": step.name, "tool_input": step.arguments, "tool_use_id": step.id}
        for output in await self._hooks(
            "PreToolUse", base | {"hook_event_name": "PreToolUse"}, step.id
        ):
            specific = output.get("hookSpecificOutput", {})
            if specific.get("permissionDecision") == "deny":
                step.denied = specific["permissionDecisionReason"]
                return
        try:
            step.response = await self.executor(step.name, step.arguments)
        except ToolError as exc:
            step.error = str(exc)
            await self._hooks(
                "PostToolUseFailure",
                base | {"hook_event_name": "PostToolUseFailure", "error": step.error},
                step.id,
            )
            return
        await self._hooks(
            "PostToolUse",
            base
            | {"hook_event_name": "PostToolUse", "tool_response": step.response, "duration_ms": 5},
            step.id,
        )

    async def _stream(self) -> AsyncIterator[Any]:
        yield init_message()
        for step in self.calls:
            await self._run(step)
            yield tool_use(step.id, step.name, **step.arguments)
            yield tool_result(step.id, is_error=step.denied is not None or step.error is not None)
        yield result_message(self.output)


class MemoryJournal:
    """Journal en mémoire, pour les tests sans base."""

    def __init__(self) -> None:
        self.calls: list[CallEntry] = []
        self.pending: dict[str, tuple[uuid.UUID, PendingEntry]] = {}

    def record_call(self, entry: CallEntry) -> None:
        self.calls.append(entry)

    def hold(self, entry: PendingEntry) -> tuple[uuid.UUID, bool]:
        if entry.idempotency_key in self.pending:
            return self.pending[entry.idempotency_key][0], False
        pending_id = uuid.uuid4()
        self.pending[entry.idempotency_key] = (pending_id, entry)
        return pending_id, True

    def has_pending(self, ticket_id: int) -> bool:
        return any(entry.ticket_id == ticket_id for _, entry in self.pending.values())


# --- Base de test ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def agentpilot_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        # Schéma recréé : pas de migrations, les tables peuvent avoir changé
        with engine.begin() as connection:
            connection.execute(DropSchema(SCHEMA, cascade=True, if_exists=True))
        init_db(engine)
    except OperationalError as exc:
        pytest.fail(
            f"Base de test injoignable ({TEST_DATABASE_URL}). "
            f"Lancer `docker compose up -d db`.\n{exc}",
            pytrace=False,
        )
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(agentpilot_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions liées à une transaction annulée en fin de test."""
    with agentpilot_engine.connect() as connection:
        transaction = connection.begin()
        # Sans rechargement après commit : sinon la session de la requête rouvrirait un point
        # de sauvegarde, et sa fermeture annulerait ce que la tâche de fond y a écrit
        # (artefact de la connexion partagée des tests ; en production, une connexion par
        # session)
        yield sessionmaker(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        transaction.rollback()
