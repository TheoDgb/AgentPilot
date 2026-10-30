"""Endpoint Chaos : jeton, cycle de vie des pannes et symptôme de chacune."""

import random
import time
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from shopsim.app import chaos
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.db import Base, add_enum_values, init_db
from shopsim.app.models import ChaosFault, ChaosScenario, Refund
from shopsim.tests.conftest import JsonLogs, MakeOrder, MakePayment, MakeTicket, SimClock

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


# --- db_errors_intermittent ------------------------------------------------------------


def draw(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr(chaos, "_draw", lambda: value)


def test_db_errors_intermittent_fails_on_a_low_draw_only(
    client: TestClient,
    make_payment: MakePayment,
    json_logs: JsonLogs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = make_payment().order
    activate(client, "db_errors_intermittent")

    draw(monkeypatch, 0.149)
    assert client.get(f"/orders/{order.id}").status_code == 500
    assert client.get(f"/customers/{order.customer_id}/orders").status_code == 500
    draw(monkeypatch, 0.15)
    assert client.get(f"/orders/{order.id}").status_code == 200
    assert client.get(f"/customers/{order.customer_id}/orders").status_code == 200

    # Même erreur, mot pour mot, que db_errors
    errors = [line for line in json_logs.read("shopsim") if line["event"] == "http.exception"]
    assert [e["error_type"] for e in errors] == ["psycopg.OperationalError"] * 2
    assert "server closed the connection unexpectedly" in errors[0]["error"]


def test_db_errors_intermittent_touches_order_reads_only(
    client: TestClient, make_payment: MakePayment, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = make_payment()
    activate(client, "db_errors_intermittent")
    draw(monkeypatch, 0.0)

    assert client.get(f"/payments/{payment.order_id}").status_code == 200
    assert client.get(f"/customers/{payment.order.customer_id}").status_code == 200
    assert post_refund(client, payment.id, "chaos-intermittent").status_code == 201


def test_db_errors_intermittent_share(
    client: TestClient, make_payment: MakePayment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tirage réel, graine fixe : une part proche de 15 %, jamais tout ni rien."""
    order = make_payment().order
    monkeypatch.setattr(chaos, "_draw", random.Random(7).random)
    activate(client, "db_errors_intermittent")

    codes = [client.get(f"/orders/{order.id}").status_code for _ in range(200)]

    assert 0.08 < codes.count(500) / len(codes) < 0.22
    assert set(codes) == {200, 500}


def test_no_intermittent_error_when_inactive(
    client: TestClient, make_payment: MakePayment, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = make_payment().order
    draw(monkeypatch, 0.0)

    assert client.get(f"/orders/{order.id}").status_code == 200
    activate(client, "db_errors_intermittent")
    stop(client, "db_errors_intermittent")
    assert client.get(f"/orders/{order.id}").status_code == 200


def test_new_fault_values_are_added_to_an_existing_database(db_engine: Engine) -> None:
    """Type d'avant la panne : la valeur manquante est ajoutée, deux fois sans erreur."""
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP TYPE IF EXISTS chaos_upgrade_test"))
        connection.execute(text("CREATE TYPE chaos_upgrade_test AS ENUM ('db_errors')"))
        try:
            for _ in range(2):
                add_enum_values(connection, "chaos_upgrade_test", ["db_errors", "intermittent"])
            labels = connection.scalars(
                text("SELECT unnest(enum_range(NULL::chaos_upgrade_test))::text")
            ).all()
        finally:
            connection.execute(text("DROP TYPE chaos_upgrade_test"))

    assert labels == ["db_errors", "intermittent"]


def test_init_db_twice_keeps_every_fault(db_engine: Engine) -> None:
    init_db(db_engine)
    init_db(db_engine)

    with db_engine.connect() as connection:
        labels = connection.scalars(
            text("SELECT unnest(enum_range(NULL::chaos_scenario))::text")
        ).all()
    assert labels == [s.value for s in ChaosScenario]


# --- connection_saturation -------------------------------------------------------------


def test_connection_saturation_rises_with_elapsed_time(
    client: TestClient, make_order: MakeOrder, slept: list[float], sim_clock: SimClock
) -> None:
    """De 0 à 5 s en 10 min de temps réel, puis le plafond : selon le temps écoulé depuis
    l'activation, jamais selon le nombre de requêtes."""
    order = make_order()
    activate(client, "connection_saturation")

    delays = []
    for minutes in (0, 0.5, 1, 5, 10, 20):
        sim_clock.elapse(timedelta(minutes=minutes))
        slept.clear()
        assert client.get(f"/orders/{order.id}").status_code == 200
        delays.append(sum(slept))

    assert delays == [0, 0.25, 0.5, 2.5, 5.0, 5.0]


def test_connection_saturation_does_not_depend_on_throughput(
    client: TestClient, make_order: MakeOrder, slept: list[float], sim_clock: SimClock
) -> None:
    """Vingt requêtes au même instant attendent la même durée : un générateur plus rapide ne
    fait pas monter la panne plus vite."""
    order = make_order()
    activate(client, "connection_saturation")
    sim_clock.elapse(timedelta(minutes=2))

    for _ in range(20):
        client.get(f"/orders/{order.id}")

    assert slept == [1.0] * 20


def test_connection_saturation_stops_and_restarts_from_zero(
    client: TestClient, make_order: MakeOrder, slept: list[float], sim_clock: SimClock
) -> None:
    order = make_order()
    activate(client, "connection_saturation")
    sim_clock.elapse(timedelta(minutes=8))
    client.get(f"/orders/{order.id}")
    assert slept == [4.0]

    stop(client, "connection_saturation")
    slept.clear()
    client.get(f"/orders/{order.id}")
    assert slept == []

    # Une nouvelle activation repart de zéro
    activate(client, "connection_saturation")
    sim_clock.elapse(timedelta(minutes=9))
    client.get(f"/orders/{order.id}")
    assert slept == [0.5]


def test_a_fault_created_without_real_start_gets_the_real_time(
    session: Session, sim_clock: SimClock
) -> None:
    """Toute panne créée sans `started_real_at` (tests, scripts) reste valide : l'heure réelle
    de la création, par `clock.real_now`."""
    sim_clock.elapse(timedelta(minutes=3))
    session.add(ChaosFault(scenario=ChaosScenario.DB_ERRORS, activated_at=SIMULATION_NOW))
    session.flush()

    fault = session.get(ChaosFault, ChaosScenario.DB_ERRORS)
    assert fault is not None
    assert fault.started_real_at == SimClock.REAL_START + timedelta(minutes=3)


def test_connection_saturation_rises_even_with_a_frozen_clock(
    client: TestClient,
    make_order: MakeOrder,
    slept: list[float],
    sim_clock: SimClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Horloge de la simulation figée (évaluation) : la date simulée ne bouge plus, mais la
    panne suit le temps réel."""
    monkeypatch.setenv("SHOPSIM_FREEZE_CLOCK", "1")
    order = make_order()
    activate(client, "connection_saturation")
    sim_clock.elapse(timedelta(minutes=5))

    client.get(f"/orders/{order.id}")

    assert slept == [2.5]


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
