"""Remise à zéro complète (`python -m agentpilot.reset`) et verrou du service.

Le vrai seed de ShopSim n'est jamais lancé ici : il réécrirait `evals/tickets.jsonl` et les
tables de ShopSim des autres tests. Il est remplacé par un compteur.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import app as app_module
from agentpilot import control, reset
from agentpilot.app import ServiceAlreadyRunning, app
from agentpilot.db import service_lock
from agentpilot.models import (
    AgentKind,
    CallOutcome,
    PendingAction,
    PendingStatus,
    Run,
    RunStatus,
    ToolCall,
)


class Seed:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def seed() -> Seed:
    return Seed()


def fill(factory: sessionmaker[Session]) -> None:
    """Une exécution, un appel au journal, une action en attente, des interrupteurs basculés."""
    now = datetime.now(UTC)
    with factory() as session:
        run = Run(
            agent=AgentKind.SUPPORT,
            ticket_id=19,
            status=RunStatus.SUCCEEDED,
            created_at=now,
            finished_at=now,
        )
        session.add(run)
        session.flush()
        session.add(
            ToolCall(
                run_id=run.id,
                seq=1,
                tool="mcp__shopsim__escalate_ticket",
                input={"ticket_id": 19},
                outcome=CallOutcome.SUCCEEDED,
                created_at=now,
            )
        )
        session.add(
            PendingAction(
                run_id=run.id,
                ticket_id=19,
                tool="refund_order",
                params={"ticket_id": 19, "order_id": 7, "amount_cents": 38_509},
                reasons=["over_threshold"],
                facts={},
                idempotency_key="test-reset-19",
                status=PendingStatus.PENDING,
                created_at=now,
            )
        )
        session.commit()
    control.resume(factory, "Alice")
    control.set_emergency(factory, True, "Alice")


def counts(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with factory() as session:
        return tuple(  # type: ignore[return-value]
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in (Run, ToolCall, PendingAction)
        )


def test_reset_seeds_shopsim_and_empties_agentpilot(
    agentpilot_engine: Engine, session_factory: sessionmaker[Session], seed: Seed
) -> None:
    fill(session_factory)

    assert reset.main(["--yes"], bind=agentpilot_engine, seed=seed) == 0

    assert seed.calls == 1
    assert counts(session_factory) == (0, 0, 0)
    settings = control.load(session_factory)
    assert settings.loop_paused and settings.pause_reason is None
    assert not settings.emergency_stop and settings.emergency_by is None


def test_reset_refuses_while_the_service_holds_the_lock(
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    seed: Seed,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fill(session_factory)

    with service_lock(agentpilot_engine) as locked:
        assert locked
        assert reset.main(["--yes"], bind=agentpilot_engine, seed=seed) == 1

    assert "Le service AgentPilot est lancé" in capsys.readouterr().err
    assert seed.calls == 0
    assert counts(session_factory) == (1, 1, 1)
    # Verrou rendu à la sortie du bloc : la remise à zéro passe
    assert reset.main(["--yes"], bind=agentpilot_engine, seed=seed) == 0


def test_reset_asks_for_confirmation(
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill(session_factory)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert reset.main([], bind=agentpilot_engine, seed=seed) == 1

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert reset.main([], bind=agentpilot_engine, seed=seed) == 1
    assert seed.calls == 0
    assert counts(session_factory) == (1, 1, 1)

    monkeypatch.setattr("builtins.input", lambda prompt: "oui")
    assert reset.main([], bind=agentpilot_engine, seed=seed) == 0
    assert seed.calls == 1


def test_running_service_holds_the_lock(
    agentpilot_engine: Engine,
    session_factory: sessionmaker[Session],
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le vrai service (lifespan complet) tient le verrou : remise à zéro et second service
    refusés tant qu'il tourne, permis après son arrêt."""
    monkeypatch.setattr(app_module, "engine", agentpilot_engine)
    monkeypatch.setenv("SHOPSIM_URL", "http://127.0.0.1:9")
    app.state.session_factory = session_factory

    with TestClient(app):
        assert reset.main(["--yes"], bind=agentpilot_engine, seed=seed) == 1
        with pytest.raises(ServiceAlreadyRunning), TestClient(app):
            pass

    assert seed.calls == 0
    assert reset.main(["--yes"], bind=agentpilot_engine, seed=seed) == 0
