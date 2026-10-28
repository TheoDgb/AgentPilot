"""Réception des alertes d'Alertmanager : enregistrées en base, visibles sur /control et
`GET /alerts`, jamais d'action automatique. Corps au format webhook d'Alertmanager (version 4)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.app import app
from agentpilot.models import Alert, AlertStatus, Run
from observability.logs import configure
from shopsim.tests.conftest import JsonLogs

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
ROUTE = "/orders/{order_id}"
# Fin d'une alerte encore active, telle qu'Alertmanager l'envoie
ZERO_TIME = "0001-01-01T00:00:00Z"


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app.state.session_factory = session_factory
    # Pas de `with` : le lifespan (base principale, boucle) n'est pas exécuté
    yield TestClient(app)


def alert(
    status: str = "firing",
    *,
    name: str = "ShopSimHighErrorRate",
    route: str | None = ROUTE,
    starts: datetime = T0,
    ends: datetime | None = None,
    fingerprint: str = "4f1c2b9e8d7a6b5c",
) -> dict[str, Any]:
    labels = {"alertname": name, "severity": "critical"}
    if route is not None:
        labels["route"] = route
    return {
        "status": status,
        "labels": labels,
        "annotations": {
            "summary": f"Erreurs 5xx sur {route}",
            "description": "Plus de 5 % des requêtes échouent (5xx) depuis 2 minutes.",
        },
        "startsAt": starts.isoformat().replace("+00:00", "Z"),
        "endsAt": ends.isoformat().replace("+00:00", "Z") if ends else ZERO_TIME,
        "generatorURL": "http://localhost:9090/graph?g0.expr=...",
        "fingerprint": fingerprint,
    }


def webhook(*alerts: dict[str, Any]) -> dict[str, Any]:
    status = "firing" if any(a["status"] == "firing" for a in alerts) else "resolved"
    return {
        "receiver": "agentpilot",
        "status": status,
        "alerts": list(alerts),
        "groupLabels": {"alertname": "ShopSimHighErrorRate", "route": ROUTE},
        "commonLabels": {"alertname": "ShopSimHighErrorRate"},
        "commonAnnotations": {},
        "externalURL": "http://localhost:9093",
        "version": "4",
        "groupKey": '{}:{alertname="ShopSimHighErrorRate", route="/orders/{order_id}"}',
        "truncatedAlerts": 0,
    }


def send(client: TestClient, *alerts: dict[str, Any]) -> None:
    response = client.post("/alerts/webhook", json=webhook(*alerts))
    assert response.status_code == 200, response.text
    assert response.json() == {"received": len(alerts)}


def rows(factory: sessionmaker[Session]) -> list[Alert]:
    with factory() as session:
        return list(session.scalars(select(Alert).order_by(Alert.starts_at)))


def test_firing_alert_is_recorded_and_shown(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert())

    [row] = rows(session_factory)
    assert (row.alertname, row.severity, row.route) == ("ShopSimHighErrorRate", "critical", ROUTE)
    assert row.status is AlertStatus.FIRING
    assert row.starts_at == T0 and row.ends_at is None
    assert row.summary == f"Erreurs 5xx sur {ROUTE}"
    [listed] = client.get("/alerts", params={"status": "firing"}).json()
    assert listed["alertname"] == "ShopSimHighErrorRate"
    [active] = client.get("/control/status").json()["active_alerts"]
    assert active["route"] == ROUTE
    page = client.get("/control").text
    assert "Alertes actives" in page and "ShopSimHighErrorRate" in page and ROUTE in page


def test_reminder_does_not_duplicate(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert())
    [first] = rows(session_factory)

    send(client, alert())

    [row] = rows(session_factory)
    assert row.id == first.id
    assert row.last_received_at > first.last_received_at
    assert row.first_received_at == first.first_received_at


def test_resolution_closes_the_alert(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert())

    send(client, alert("resolved", ends=T0 + timedelta(minutes=7)))

    [row] = rows(session_factory)
    assert row.status is AlertStatus.RESOLVED
    assert row.ends_at == T0 + timedelta(minutes=7)
    assert client.get("/control/status").json()["active_alerts"] == []
    assert "Aucune alerte active" in client.get("/control").text


def test_resolution_without_firing_is_recorded(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """AgentPilot était arrêté pendant l'alerte (AgentPilotDown) : seule la fin lui arrive."""
    send(
        client,
        alert("resolved", name="AgentPilotDown", route=None, ends=T0 + timedelta(minutes=5)),
    )

    [row] = rows(session_factory)
    assert row.status is AlertStatus.RESOLVED
    assert (row.starts_at, row.route) == (T0, None)


def test_late_reminder_does_not_reopen_a_resolved_alert(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert("resolved", ends=T0 + timedelta(minutes=7)))

    send(client, alert())

    [row] = rows(session_factory)
    assert row.status is AlertStatus.RESOLVED
    assert row.ends_at == T0 + timedelta(minutes=7)


def test_new_occurrence_after_resolution_is_a_new_row(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert())
    send(client, alert("resolved", ends=T0 + timedelta(minutes=7)))

    send(client, alert(starts=T0 + timedelta(hours=1)))

    first, second = rows(session_factory)
    assert (first.status, second.status) == (AlertStatus.RESOLVED, AlertStatus.FIRING)
    assert len(client.get("/control/status").json()["active_alerts"]) == 1
    assert [a["status"] for a in client.get("/alerts").json()] == ["firing", "resolved"]


def test_several_alerts_in_one_webhook(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(
        client,
        alert(),
        alert(route="/customers/{customer_id}/orders", fingerprint="9a8b7c6d5e4f3a2b"),
    )

    assert {row.route for row in rows(session_factory)} == {
        ROUTE,
        "/customers/{customer_id}/orders",
    }


@pytest.mark.parametrize(
    "broken",
    [
        {"status": "burning"},
        {"fingerprint": ""},
        {"startsAt": "hier"},
        {"labels": "ShopSimHighErrorRate"},
    ],
)
def test_invalid_webhook_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session], broken: dict[str, Any]
) -> None:
    response = client.post("/alerts/webhook", json=webhook(alert() | broken))

    assert response.status_code == 422
    assert rows(session_factory) == []


def test_alerts_never_launch_anything(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    send(client, alert(), alert(name="ShopSimDown", route=None, fingerprint="1234abcd"))

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_each_alert_is_logged(client: TestClient, json_logs: JsonLogs) -> None:
    configure("agentpilot", json_logs.dir, stream=json_logs.stderr)

    send(client, alert(), alert("resolved", fingerprint="77aa", ends=T0 + timedelta(minutes=3)))

    received = [line for line in json_logs.read("agentpilot") if line["event"] == "alert.received"]
    assert [(line["status"], line["level"]) for line in received] == [
        ("firing", "warning"),
        ("resolved", "info"),
    ]
    assert received[0]["alertname"] == "ShopSimHighErrorRate"
    assert received[0]["severity"] == "critical"
    assert received[0]["route"] == ROUTE
    assert received[0]["trace_id"] is not None
