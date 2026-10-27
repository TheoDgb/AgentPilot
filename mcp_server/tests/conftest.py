"""Fixtures : ShopSim tourne vraiment (uvicorn dans un thread) sur la base de test.

La base, la transaction annulée en fin de test et les fabriques `make_*` sont celles
des tests de ShopSim. Le serveur MCP n'y accède que par HTTP, comme en production.
"""

import itertools
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime

import pytest
import uvicorn
from sqlalchemy.orm import Session

from shopsim.app.db import get_session
from shopsim.app.main import app
from shopsim.app.models import Payment, Refund
from shopsim.tests import conftest as shopsim_fixtures

# Fixtures des tests de ShopSim, réexportées : base de test, transaction annulée, `make_*`
db_engine = shopsim_fixtures.db_engine
session = shopsim_fixtures.session
no_payment_latency = shopsim_fixtures.no_payment_latency
make_customer = shopsim_fixtures.make_customer
make_order = shopsim_fixtures.make_order
make_payment = shopsim_fixtures.make_payment
make_ticket = shopsim_fixtures.make_ticket
make_shipment = shopsim_fixtures.make_shipment
sim_clock = shopsim_fixtures.sim_clock
json_logs = shopsim_fixtures.json_logs

MakeShipment = shopsim_fixtures.MakeShipment
MakeRefund = Callable[..., Refund]
_unique = itertools.count(1)


@pytest.fixture
def anyio_backend() -> str:
    """Tests asynchrones (outils MCP) sur asyncio."""
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def shopsim_server() -> Iterator[str]:
    """ShopSim servi en HTTP sur un port libre, sans lifespan (pas de create_all)."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("ShopSim n'a pas démarré dans le thread uvicorn", pytrace=False)
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def shopsim(
    shopsim_server: str, session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """ShopSim lit et écrit dans la session du test ; le serveur MCP pointe dessus."""
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setenv("SHOPSIM_URL", shopsim_server)
    yield shopsim_server
    app.dependency_overrides.clear()


@pytest.fixture
def make_refund(session: Session) -> MakeRefund:
    def _make(payment: Payment, amount_cents: int, created_at: datetime) -> Refund:
        refund = Refund(
            payment=payment,
            amount_cents=amount_cents,
            reason="Produit défectueux",
            idempotency_key=f"test-mcp-refund-{next(_unique)}",
            created_at=created_at,
        )
        session.add(refund)
        session.flush()
        return refund

    return _make
