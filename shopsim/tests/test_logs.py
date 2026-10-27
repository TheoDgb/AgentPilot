"""Logs JSON de ShopSim : une ligne par requête, trace reprise ou créée, pannes sans marqueur,
requêtes Chaos à part, ni jeton ni texte de client."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from observability.trace import parse_traceparent, traceparent
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.db import engine
from shopsim.app.main import app
from shopsim.app.models import MessageAuthor
from shopsim.tests.conftest import JsonLogs, MakeCustomer, MakePayment, MakeTicket

TOKEN = "test-chaos-token-0123"
AUTH = {"X-Chaos-Token": TOKEN}
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
# Mots qui trahiraient une panne injectée
MARKERS = ("chaos", "simul", "panne", "inject", "fault", "db_errors", "bad_deploy")


@pytest.fixture(autouse=True)
def chaos_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", TOKEN)


@pytest.fixture
def failing_route() -> Iterator[str]:
    """Route qui lève une vraie exception non gérée, retirée après le test."""

    def boom() -> None:
        raise KeyError("REFUND_PROVIDER_URL")

    app.add_api_route("/test-only/boom", boom)
    yield "/test-only/boom"
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", "") != "/test-only/boom"
    ]


def requests(logs: JsonLogs) -> list[dict]:
    return [line for line in logs.read("shopsim") if line["event"] == "http.request"]


def test_each_request_is_one_json_line(
    client: TestClient, json_logs: JsonLogs, make_payment: MakePayment
) -> None:
    payment = make_payment()

    response = client.get(f"/orders/{payment.order_id}", params={"x": "1"})

    (line,) = requests(json_logs)
    assert line["service"] == "shopsim"
    assert line["level"] == "info"
    assert line["method"] == "GET"
    assert line["route"] == "/orders/{order_id}"
    assert line["path"] == f"/orders/{payment.order_id}"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int)
    # Trace créée, renvoyée au client
    assert len(line["trace_id"]) == 32
    assert parse_traceparent(response.headers["traceparent"]) == line["trace_id"]
    # Aussi sur stderr (docker compose logs), même ligne
    assert json_logs.stderr_lines() == [line]
    # Jamais la chaîne de requête
    assert "x=1" not in json_logs.everything()


def test_incoming_trace_is_kept(client: TestClient, json_logs: JsonLogs) -> None:
    response = client.get("/clock", headers={"traceparent": traceparent(TRACE)})

    (line,) = requests(json_logs)
    assert line["trace_id"] == TRACE
    assert parse_traceparent(response.headers["traceparent"]) == TRACE


@pytest.mark.parametrize("header", ["", "invalide", f"00-{'0' * 32}-{'1' * 16}-01"])
def test_missing_or_invalid_trace_is_replaced(
    client: TestClient, json_logs: JsonLogs, header: str
) -> None:
    client.get("/clock", headers={"traceparent": header} if header else {})

    (line,) = requests(json_logs)
    assert len(line["trace_id"]) == 32
    assert line["trace_id"] != "0" * 32


def test_client_errors_are_warnings(client: TestClient, json_logs: JsonLogs) -> None:
    client.get("/orders/999999999")

    (line,) = requests(json_logs)
    assert (line["status"], line["level"]) == (404, "warning")


def test_fault_errors_look_like_real_errors(
    client: TestClient, json_logs: JsonLogs, make_payment: MakePayment, failing_route: str
) -> None:
    """Les logs ne trahissent pas la panne : même événement, même logger, mêmes champs
    qu'une vraie erreur, sans aucun mot qui la désigne. Seul `GET /chaos` la révèle."""
    payment = make_payment()
    client.post("/chaos/db_errors", headers=AUTH)
    client.post("/chaos/bad_deploy", headers=AUTH)

    assert client.get(f"/orders/{payment.order_id}").status_code == 500
    refund = client.post(
        "/refunds",
        json={"payment_id": payment.id, "amount_cents": 100, "reason": "Retour"},
        headers={"Idempotency-Key": "logs-bad-deploy"},
    )
    assert refund.status_code == 500
    real = client.get(failing_route)
    assert real.status_code == 500
    # Même réponse qu'une erreur de panne
    assert real.json() == refund.json() == {"detail": "Internal Server Error"}

    lines = json_logs.read("shopsim")
    errors = [line for line in lines if line["event"] == "http.exception"]
    db_error, deploy_error, real_error = errors
    assert db_error["error_type"] == "psycopg.OperationalError"
    assert "server closed the connection unexpectedly" in db_error["error"]
    assert (deploy_error["error_type"], deploy_error["error"]) == (
        "KeyError",
        "'REFUND_PROVIDER_URL'",
    )
    assert real_error["error_type"] == "KeyError"
    # Champs d'une panne : ceux d'une vraie erreur (la vraie a en plus sa traceback)
    for fault in (db_error, deploy_error):
        assert fault.keys() <= real_error.keys()
        assert real_error.keys() - fault.keys() == {"traceback"}
        assert (fault["level"], fault["logger"]) == (real_error["level"], real_error["logger"])
    assert [line["status"] for line in requests(json_logs)] == [500, 500, 500]
    text = json_logs.read("shopsim").__repr__().lower() + json_logs.stderr.getvalue().lower()
    for marker in MARKERS:
        assert marker not in text


def test_chaos_requests_go_only_to_their_own_file(
    client: TestClient, json_logs: JsonLogs, capsys: pytest.CaptureFixture[str]
) -> None:
    client.post("/chaos/db_errors", headers=AUTH)
    client.get("/chaos", headers=AUTH)
    client.delete("/chaos/db_errors", headers=AUTH)
    client.post("/chaos/db_errors", headers={"X-Chaos-Token": "mauvais-jeton"})
    client.post("/chaos/unknown_scenario", headers=AUTH)

    chaos = json_logs.read("chaos")
    assert [(line["method"], line["path"], line["status"]) for line in chaos] == [
        ("POST", "/chaos/db_errors", 201),
        ("GET", "/chaos", 200),
        ("DELETE", "/chaos/db_errors", 204),
        ("POST", "/chaos/db_errors", 403),
        ("POST", "/chaos/unknown_scenario", 422),
    ]
    assert json_logs.read("shopsim") == []
    assert json_logs.stderr.getvalue() == ""
    captured = capsys.readouterr()
    assert "chaos" not in (captured.out + captured.err).lower()
    # Le jeton n'est écrit nulle part, pas même dans chaos.jsonl
    assert TOKEN not in json_logs.everything()
    assert "mauvais-jeton" not in json_logs.everything()


def test_customer_text_is_never_logged(
    client: TestClient,
    json_logs: JsonLogs,
    make_customer: MakeCustomer,
    make_ticket: MakeTicket,
) -> None:
    secret_message = "Mon adresse est 12 rue des Lilas, mon téléphone 06 12 34 56 78"
    ticket = make_ticket(
        make_customer(), messages=[(MessageAuthor.CUSTOMER, secret_message, SIMULATION_NOW)]
    )

    client.get(f"/tickets/{ticket.id}")
    client.get("/tickets", params={"status": "open"})
    reply = "Bonjour Madame Martin, votre colis part demain."
    client.post(
        f"/tickets/{ticket.id}/reply", json={"body": reply}, headers={"Idempotency-Key": "k-logs"}
    )
    client.post(f"/tickets/{ticket.id}/escalate", json={"reason": "Client très mécontent"})

    text = json_logs.everything()
    assert len(requests(json_logs)) == 4
    for private in ("rue des Lilas", "06 12 34 56 78", "Madame Martin", "très mécontent", "k-logs"):
        assert private not in text


def test_sql_errors_hide_their_parameters() -> None:
    """Une erreur SQL ne recopie pas les valeurs (texte d'un message) dans son message."""
    assert engine.hide_parameters is True


def test_successful_health_checks_are_not_logged(
    client: TestClient, json_logs: JsonLogs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le healthcheck Docker passe toutes les 5 s : seuls ses échecs sont écrits."""
    assert client.get("/health").status_code == 200
    assert json_logs.read("shopsim") == []

    def down(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connexion perdue"))

    monkeypatch.setattr(Session, "execute", down)
    assert client.get("/health").status_code == 503

    (line,) = requests(json_logs)
    assert (line["path"], line["status"], line["level"]) == ("/health", 503, "error")
