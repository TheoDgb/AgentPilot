"""Fixtures d'AgentPilot : faux Claude Agent SDK et base de test (schéma `agentpilot`)."""

import os
from collections.abc import AsyncIterator, Iterator
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
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import support_agent
from agentpilot.db import init_db
from agentpilot.support_agent import READ_TOOL_NAMES

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://shopsim:shopsim@localhost:5433/shopsim_test"
)
# Date de l'horloge de ShopSim (GET /clock), remplacée dans les tests
TODAY = date(2026, 7, 6)
MODEL = "claude-sonnet-5-5"
# Liste réelle observée : les 8 outils de lecture et l'outil de sortie structurée
INIT_TOOLS = ["StructuredOutput", *sorted(READ_TOOL_NAMES)]


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


# --- Base de test ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def agentpilot_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
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
