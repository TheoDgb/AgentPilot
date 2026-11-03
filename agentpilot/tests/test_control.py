"""Démarrer ne dépense rien (J7.1) : à chaque démarrage du service, boucle de l'agent Support en
pause et enquête automatique coupée, sauf `AGENTPILOT_PAUSE_ON_START=0`."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import app as app_module
from agentpilot import control
from agentpilot.app import app
from agentpilot.models import PauseReason
from agentpilot.tests.conftest import reset_agentpilot
from agentpilot.worker import Worker


@pytest.fixture
def running(
    agentpilot_engine: Engine, session_factory: sessionmaker[Session]
) -> sessionmaker[Session]:
    """Boucle laissée en marche et enquête automatique active avant l'arrêt du service."""
    reset_agentpilot(agentpilot_engine, loop_paused=False)
    control.set_ops_auto(session_factory, True, "Alice")
    return session_factory


def test_start_pauses_the_loop_and_turns_off_automatic_investigations(
    running: sessionmaker[Session],
) -> None:
    control.pause_on_start(running)

    settings = control.load(running)
    assert settings.loop_paused is True
    assert settings.pause_reason is PauseReason.MANUAL
    assert settings.paused_by == control.STARTUP
    assert settings.ops_auto is False and settings.ops_auto_by == control.STARTUP


def test_zero_leaves_everything_as_it_was(
    running: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(control.PAUSE_ON_START_ENV, "0")

    control.pause_on_start(running)

    settings = control.load(running)
    assert settings.loop_paused is False and settings.ops_auto is True


def test_an_automatic_pause_keeps_its_reason(session_factory: sessionmaker[Session]) -> None:
    """Pause pour erreur d'infrastructure : son motif reste, un humain doit régler la clé."""
    control.pause(session_factory, None, PauseReason.INFRA, "api_error")

    control.pause_on_start(session_factory)

    settings = control.load(session_factory)
    assert (settings.pause_reason, settings.pause_detail) == (PauseReason.INFRA, "api_error")


def test_the_real_service_starts_paused(
    agentpilot_engine: Engine,
    running: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan complet : la boucle laissée en marche ne repart pas d'elle-même."""
    monkeypatch.setattr(app_module, "engine", agentpilot_engine)
    monkeypatch.setenv("SHOPSIM_URL", "http://127.0.0.1:9")
    app.state.session_factory = running
    # Boucle propre au test : ShopSim injoignable la met en attente, ce qui ne doit pas
    # rester sur la boucle partagée par les tests suivants (jauge `waiting` des métriques).
    # Remise à la main : `monkeypatch` sur `app.state` (Starlette) supprime l'attribut au lieu
    # de le rendre
    shared = app.state.worker
    app.state.worker = Worker(app.state)
    try:
        with TestClient(app) as client:
            status = client.get("/control/status").json()
    finally:
        app.state.worker = shared

    assert status["loop_paused"] is True and status["ops_auto"] is False
    assert status["paused_by"] == control.STARTUP
