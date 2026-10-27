"""Commande de pilotage des pannes : contre les vrais endpoints Chaos (client de test), jeton lu
dans l'environnement ou sur sa seule ligne de `.env`, jamais affiché."""

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shopsim import chaos_cli
from shopsim.chaos_cli import Send, main

TOKEN = "test-chaos-token-0123"


@pytest.fixture(autouse=True)
def chaos_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", TOKEN)


@pytest.fixture
def send(client: TestClient) -> Send:
    def _send(method: str, path: str, token: str) -> tuple[int, Any]:
        response = client.request(method, path, headers={"X-Chaos-Token": token})
        return response.status_code, response.json() if response.content else None

    return _send


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f'ANTHROPIC_API_KEY=sk-ant-secret\nCHAOS_TOKEN="{TOKEN}"\nOTHER=1\n', encoding="utf-8"
    )
    return path


def run(send: Send, env_file: Path, *argv: str, environ: dict[str, str] | None = None) -> int:
    return main(list(argv), environ=environ or {}, env_file=env_file, send=send)


def test_on_status_off(send: Send, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(send, env_file, "status") == 0
    assert "Aucune panne active" in capsys.readouterr().out

    assert run(send, env_file, "on", "payment_latency") == 0
    assert "payment_latency activée : Paiements répond en 4 s" in capsys.readouterr().out
    assert run(send, env_file, "on", "payment_latency") == 0
    assert "déjà active" in capsys.readouterr().out
    assert run(send, env_file, "status") == 0
    assert "payment_latency active depuis" in capsys.readouterr().out

    assert run(send, env_file, "off", "payment_latency") == 0
    assert "payment_latency arrêtée" in capsys.readouterr().out
    run(send, env_file, "status")
    assert "Aucune panne active" in capsys.readouterr().out


def test_off_all(send: Send, env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(send, env_file, "on", "db_errors")
    run(send, env_file, "on", "bad_deploy")

    assert run(send, env_file, "off", "all") == 0

    capsys.readouterr()
    run(send, env_file, "status")
    assert "Aucune panne active" in capsys.readouterr().out


def test_token_read_from_its_line_only(env_file: Path) -> None:
    before = dict(os.environ)

    assert chaos_cli.read_token({}, env_file) == TOKEN
    # Rien du fichier n'entre dans l'environnement
    assert dict(os.environ) == before
    # Une variable du shell l'emporte sur .env
    assert chaos_cli.read_token({"CHAOS_TOKEN": "from-shell"}, env_file) == "from-shell"
    assert chaos_cli.read_token({}, env_file.parent / "absent") is None


def test_missing_token_sends_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / ".env"
    empty.write_text("CHAOS_TOKEN=\n", encoding="utf-8")

    def never(method: str, path: str, token: str) -> tuple[int, Any]:
        raise AssertionError("aucune requête sans jeton")

    assert main(["status"], environ={}, env_file=empty, send=never) == 1
    assert "CHAOS_TOKEN absent" in capsys.readouterr().err


def test_wrong_token_is_explained_and_never_printed(
    send: Send, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong = tmp_path / ".env"
    wrong.write_text("CHAOS_TOKEN=mauvais-jeton-secret\n", encoding="utf-8")

    assert run(send, wrong, "on", "db_errors") == 1

    out = capsys.readouterr()
    assert "Jeton refusé" in out.err
    assert "mauvais-jeton-secret" not in out.out + out.err


def test_unknown_scenario_is_refused(env_file: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        run(chaos_cli.http_sender("http://127.0.0.1:1"), env_file, "on", "meteor")
    assert exc.value.code == 2


def test_unreachable_shopsim(env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    send = chaos_cli.http_sender("http://127.0.0.1:1")

    assert run(send, env_file, "status") == 1
    assert "ShopSim injoignable à http://127.0.0.1:1" in capsys.readouterr().err
