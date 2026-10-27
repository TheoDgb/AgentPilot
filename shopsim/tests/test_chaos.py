"""Endpoint Chaos : jeton, cycle de vie des pannes et symptôme de chacune."""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.db import Base
from shopsim.app.models import ChaosFault, ChaosScenario, Refund
from shopsim.tests.conftest import MakeOrder, MakePayment, MakeTicket

TOKEN = "test-chaos-token"
AUTH = {"X-Chaos-Token": TOKEN}


@pytest.fixture(autouse=True)
def chaos_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", TOKEN)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Durées d'attente demandées, sans réellement attendre."""
    durations: list[float] = []
    monkeypatch.setattr(time, "sleep", durations.append)
    yield durations


def activate(client: TestClient, scenario: str) -> Response:
    return client.post(f"/chaos/{scenario}", headers=AUTH)


def stop(client: TestClient, scenario: str) -> Response:
    return client.delete(f"/chaos/{scenario}", headers=AUTH)


def active(client: TestClient) -> list[str]:
    return [f["scenario"] for f in client.get("/chaos", headers=AUTH).json()["active"]]


def post_refund(client: TestClient, payment_id: int, key: str) -> Response:
    return client.post(
        "/refunds",
        json={"payment_id": payment_id, "amount_cents": 100, "reason": "Test chaos"},
        headers={"Idempotency-Key": key},
    )


# --- Jeton -----------------------------------------------------------------------------

ROUTES = [("get", "/chaos"), ("post", "/chaos/db_errors"), ("delete", "/chaos/db_errors")]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-Chaos-Token": "mauvais-jeton"}])
def test_chaos_requires_valid_token(
    client: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = client.request(method, path, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid chaos token"}
    assert active(client) == []


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_chaos_is_closed_without_server_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", "")

    assert client.request(method, path, headers={"X-Chaos-Token": ""}).status_code == 403


# --- Cycle de vie ----------------------------------------------------------------------


def test_activate_lists_and_stop_fault(client: TestClient) -> None:
    created = activate(client, "db_errors")

    assert created.status_code == 201
    assert created.json()["scenario"] == "db_errors"
    assert active(client) == ["db_errors"]

    stopped = stop(client, "db_errors")

    assert stopped.status_code == 204
    assert active(client) == []


def test_activate_twice_is_a_no_op(client: TestClient) -> None:
    first = activate(client, "bad_deploy")

    again = activate(client, "bad_deploy")

    assert again.status_code == 200
    assert again.json() == first.json()
    assert active(client) == ["bad_deploy"]


def test_stop_inactive_fault_is_a_no_op(client: TestClient) -> None:
    assert stop(client, "payment_latency").status_code == 204
    assert stop(client, "payment_latency").status_code == 204


def test_several_faults_can_be_active(client: TestClient) -> None:
    for scenario in ChaosScenario:
        activate(client, scenario)

    assert sorted(active(client)) == sorted(ChaosScenario)


def test_unknown_scenario_is_rejected(client: TestClient) -> None:
    assert activate(client, "meteor_strike").status_code == 422


def test_fault_table_is_recreated_by_the_seed() -> None:
    """Le seed recrée toutes les tables : il arrête donc toutes les pannes."""
    assert "chaos_faults" in Base.metadata.tables


# --- payment_latency -------------------------------------------------------------------


def test_payment_latency(client: TestClient, make_payment: MakePayment, slept: list[float]) -> None:
    payment = make_payment()
    activate(client, "payment_latency")

    assert client.get(f"/payments/{payment.order_id}").status_code == 200
    assert slept == [4.0]

    stop(client, "payment_latency")
    slept.clear()

    assert client.get(f"/payments/{payment.order_id}").status_code == 200
    # Retour à la latence normale (PAYMENT_LATENCY_MS=0 dans les tests)
    assert slept == [0.0]


# --- db_errors -------------------------------------------------------------------------


def test_db_errors(client: TestClient, make_payment: MakePayment, make_ticket: MakeTicket) -> None:
    payment = make_payment()
    order = payment.order
    ticket = make_ticket(order.customer, order)
    activate(client, "db_errors")

    assert client.get(f"/orders/{order.id}").status_code == 500
    assert client.get(f"/customers/{order.customer_id}/orders").status_code == 500
    # Les autres services ne sont pas touchés
    assert client.get(f"/payments/{order.id}").status_code == 200
    assert client.get(f"/tickets/{ticket.id}").status_code == 200

    stop(client, "db_errors")

    assert client.get(f"/orders/{order.id}").status_code == 200
    assert client.get(f"/customers/{order.customer_id}/orders").status_code == 200


# --- connection_saturation -------------------------------------------------------------


def test_connection_saturation(
    client: TestClient, make_order: MakeOrder, slept: list[float]
) -> None:
    order = make_order()
    activate(client, "connection_saturation")

    for _ in range(3):
        assert client.get(f"/orders/{order.id}").status_code == 200

    # Le temps de réponse augmente avec le nombre de requêtes
    assert slept == [0.05, 0.1, 0.15]

    stop(client, "connection_saturation")
    slept.clear()
    client.get(f"/orders/{order.id}")

    assert slept == []

    # Une nouvelle activation repart de zéro
    activate(client, "connection_saturation")
    client.get(f"/orders/{order.id}")

    assert slept == [0.05]


def test_connection_saturation_is_capped(
    client: TestClient, make_order: MakeOrder, slept: list[float], session: Session
) -> None:
    order = make_order()
    activate(client, "connection_saturation")
    fault = session.get(ChaosFault, ChaosScenario.CONNECTION_SATURATION)
    assert fault is not None
    fault.request_count = 500
    session.flush()

    client.get(f"/orders/{order.id}")

    assert slept == [5.0]


# --- bad_deploy ------------------------------------------------------------------------


def test_bad_deploy(client: TestClient, make_payment: MakePayment, session: Session) -> None:
    payment = make_payment()
    activate(client, "bad_deploy")

    assert post_refund(client, payment.id, "chaos-bad-deploy").status_code == 500
    assert session.scalar(select(func.count()).select_from(Refund)) == 0
    # Les lectures fonctionnent toujours
    assert client.get(f"/payments/{payment.order_id}").status_code == 200
    assert client.get(f"/orders/{payment.order_id}").status_code == 200

    stop(client, "bad_deploy")

    assert post_refund(client, payment.id, "chaos-bad-deploy").status_code == 201


# --- Santé -----------------------------------------------------------------------------


def test_health_ignores_faults(client: TestClient, slept: list[float]) -> None:
    for scenario in ChaosScenario:
        activate(client, scenario)

    assert client.get("/health").status_code == 200
    assert slept == []
