"""Réglages `AGENTPILOT_*` lus dans `.env`, sans jamais charger le reste du fichier."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import claude_guard, control, env_file, worker
from agentpilot.app import LOG_DIR_ENV, log_dir
from agentpilot.env_file import read_value, setting
from agentpilot.sdk_runner import decide_ticket
from agentpilot.support_agent import REPO_ROOT
from agentpilot.tests.conftest import (
    DECISION,
    TEST_DATABASE_URL,
    FakeQuery,
    MemoryJournal,
    init_message,
    result_message,
)

ENV = """\
# Réglages du service
ANTHROPIC_API_KEY=sk-ant-from-file
CHAOS_TOKEN=chaos-secret
AGENTPILOT_DAILY_BUDGET_USD="12.5"
AGENTPILOT_LOOP_INTERVAL_S=7
# AGENTPILOT_DATABASE_URL=commentée
"""


@pytest.fixture
def dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`.env` de test, et aucune de ces variables dans l'environnement."""
    path = tmp_path / ".env"
    path.write_text(ENV, encoding="utf-8")
    monkeypatch.setattr(env_file, "ENV_FILE", path)
    for name in (
        "CHAOS_TOKEN",
        "ANTHROPIC_API_KEY",
        control.BUDGET_ENV,
        worker.INTERVAL_ENV,
        claude_guard.CLI_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return path


def test_settings_are_read_from_the_env_file(dotenv: Path) -> None:
    assert control.daily_budget() == 12.5
    assert worker.interval_s() == 7
    # Ligne commentée ou absente : rien
    assert setting("AGENTPILOT_DATABASE_URL") is None


def test_environment_wins_over_the_env_file(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(control.BUDGET_ENV, "3")

    assert control.daily_budget() == 3


def test_only_agentpilot_settings_can_be_read(dotenv: Path) -> None:
    with pytest.raises(ValueError, match="AGENTPILOT_"):
        setting("CHAOS_TOKEN")


def test_reading_settings_loads_nothing_into_the_environment(dotenv: Path) -> None:
    before = dict(os.environ)

    control.daily_budget()
    worker.interval_s()
    claude_guard.real_cli(os.environ)

    assert dict(os.environ) == before
    assert "CHAOS_TOKEN" not in os.environ


@pytest.mark.anyio
async def test_agent_processes_never_inherit_the_chaos_token(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La CLI reçoit `os.environ` plus `options.env` : ni l'un ni l'autre ne contient le jeton,
    même quand la clé d'API vient du même `.env`."""
    monkeypatch.setattr("agentpilot.support_agent.ENV_FILE", dotenv)
    fake = FakeQuery(init_message(), result_message(DECISION | {"ticket_id": 21}))

    result = await decide_ticket(21, journal=MemoryJournal(), environ=os.environ, query=fake)

    assert result.error is None
    assert fake.options is not None
    assert fake.options.env["ANTHROPIC_API_KEY"] == "sk-ant-from-file"
    assert "CHAOS_TOKEN" not in fake.options.env
    assert "CHAOS_TOKEN" not in os.environ
    assert "chaos-secret" not in repr(fake.options)


def test_read_value_keeps_the_last_line_and_strips_quotes(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("AGENTPILOT_X=1\nAGENTPILOT_X='2'\nAGENTPILOT_Y=\n", encoding="utf-8")

    assert read_value("AGENTPILOT_X", path) == "2"
    assert read_value("AGENTPILOT_Y", path) is None
    assert read_value("AGENTPILOT_X", tmp_path / "absent") is None


SERVICE = """\
import json, os, subprocess, sys
from pathlib import Path

from agentpilot import env_file

env_file.ENV_FILE = Path(sys.argv[1])  # avant tout import qui lit un réglage
from fastapi.testclient import TestClient

from agentpilot.app import app
from agentpilot.support_agent import load_api_key

with TestClient(app) as client:  # démarrage complet : lifespan, boucle de fond
    status = client.get("/control/status").json()
    load_api_key(os.environ)
    child = subprocess.run(["env"], capture_output=True, text=True, check=True).stdout
print(json.dumps({
    "budget": status["daily_budget_usd"],
    "in_service": "CHAOS_TOKEN" in os.environ,
    "in_child": "CHAOS_TOKEN" in child or "chaos-secret" in child,
}))
"""


def test_service_never_loads_the_chaos_token(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """Le vrai service démarre avec un `.env` qui contient CHAOS_TOKEN : il en lit ses
    réglages, mais ni lui ni un processus qu'il lance (comme la CLI) n'a le jeton."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        ENV
        + f"AGENTPILOT_DATABASE_URL={TEST_DATABASE_URL}\n"
        + f"AGENTPILOT_LOG_DIR={tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CHAOS_TOKEN", "ANTHROPIC_API_KEY") and not key.startswith("AGENTPILOT_")
    }
    # ShopSim injoignable : la boucle (en pause) ne touche à rien
    environ["SHOPSIM_URL"] = "http://127.0.0.1:9"

    done = subprocess.run(
        [sys.executable, "-c", SERVICE, str(dotenv)],
        cwd=tmp_path,
        env=environ | {"PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"budget": "12.5", "in_service": False, "in_child": False}


@pytest.mark.parametrize("value", ["logs", "./var/logs", "/srv/abs-logs"])
def test_log_dir_is_absolute_for_the_mcp_server(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Le serveur MCP tourne ailleurs que le service : un chemin relatif part du dépôt."""
    monkeypatch.setenv(LOG_DIR_ENV, value)

    found = log_dir()

    assert found.is_absolute()
    assert found == (REPO_ROOT / value if not value.startswith("/") else Path(value))
