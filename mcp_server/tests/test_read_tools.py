"""Outils MCP en lecture : appelés à travers le protocole MCP, avec ShopSim qui tourne."""

import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp_types import CallToolResult
from sqlalchemy.orm import Session

from mcp_server import read_tools, shopsim_client
from mcp_server.server import mcp
from mcp_server.tests.conftest import MakeRefund, MakeShipment
from shopsim.app.models import (
    ChaosFault,
    ChaosScenario,
    MessageAuthor,
    OrderStatus,
    ShipmentStatus,
    TicketStatus,
)
from shopsim.tests.conftest import (
    MakeCustomer,
    MakeOrder,
    MakePayment,
    MakeTicket,
    SimClock,
)

pytestmark = pytest.mark.anyio

READ_TOOL_NAMES = {
    "list_open_tickets",
    "get_ticket",
    "get_order",
    "get_customer",
    "list_customer_orders",
    "list_customer_refunds",
    "get_payment",
    "get_shipment",
}


async def call(name: str, **arguments: Any) -> CallToolResult:  # noqa: ANN401
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments)


def data(result: CallToolResult) -> Any:  # noqa: ANN401
    """Contenu structuré d'un appel réussi (une liste est enveloppée dans `result`)."""
    assert not result.is_error, result.content
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return " ".join(block.text for block in result.content if block.type == "text")


# --- Liste des outils ------------------------------------------------------------------


async def test_only_read_tools_are_exposed() -> None:
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools

    assert {tool.name for tool in tools} == READ_TOOL_NAMES
    for tool in tools:
        assert tool.description and "Returns" in tool.description and "Errors" in tool.description
        assert tool.annotations is not None and tool.annotations.read_only_hint
        assert "chaos" not in tool.name


# --- Chaque outil, ShopSim qui tourne --------------------------------------------------


async def test_list_open_tickets(
    shopsim: str, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    customer = make_customer()
    first = make_ticket(customer, created_at=datetime(2026, 7, 4, tzinfo=UTC))
    second = make_ticket(customer, created_at=datetime(2026, 7, 5, tzinfo=UTC))
    make_ticket(customer, status=TicketStatus.RESOLVED)

    page = data(await call("list_open_tickets", limit=100))

    ids = [ticket["id"] for ticket in page["items"]]
    assert ids.index(first.id) < ids.index(second.id)
    assert all(ticket["status"] == "open" for ticket in page["items"])
    assert page["limit"] == 100


async def test_get_ticket_with_messages(
    shopsim: str, make_order: MakeOrder, make_ticket: MakeTicket
) -> None:
    order = make_order()
    at = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
    ticket = make_ticket(
        order.customer,
        order,
        messages=[
            (MessageAuthor.CUSTOMER, "Où est mon colis ?", at),
            (MessageAuthor.AGENT, "Je regarde.", at),
        ],
    )

    body = data(await call("get_ticket", ticket_id=ticket.id))

    assert body["id"] == ticket.id
    assert body["order_id"] == order.id
    assert [(m["author"], m["body"]) for m in body["messages"]] == [
        ("customer", "Où est mon colis ?"),
        ("agent", "Je regarde."),
    ]


async def test_get_order(shopsim: str, make_order: MakeOrder) -> None:
    order = make_order(OrderStatus.DELIVERED, total_cents=4_990)

    body = data(await call("get_order", order_id=order.id))

    assert body["id"] == order.id
    assert body["status"] == "delivered"
    assert body["total_cents"] == 4_990


async def test_get_customer_and_orders(shopsim: str, make_order: MakeOrder) -> None:
    order = make_order()
    other = make_order(customer=order.customer, created_at=datetime(2026, 5, 1, tzinfo=UTC))

    customer = data(await call("get_customer", customer_id=order.customer_id))
    orders = data(await call("list_customer_orders", customer_id=order.customer_id))

    assert customer["id"] == order.customer_id
    assert customer["refunds_count"] == 0
    assert [o["id"] for o in orders] == [other.id, order.id]


async def test_list_customer_refunds(
    shopsim: str, make_payment: MakePayment, make_refund: MakeRefund
) -> None:
    payment = make_payment()
    make_refund(payment, 1_000, datetime(2026, 5, 1, tzinfo=UTC))
    make_refund(payment, 2_000, datetime(2026, 6, 15, tzinfo=UTC))

    refunds = data(await call("list_customer_refunds", customer_id=payment.order.customer_id))

    assert [r["amount_cents"] for r in refunds] == [2_000, 1_000]
    assert refunds[0]["order_id"] == payment.order_id
    assert refunds[0]["created_at"].startswith("2026-06-15")


async def test_get_payment(shopsim: str, make_payment: MakePayment) -> None:
    payment = make_payment(amount_cents=7_500)

    body = data(await call("get_payment", order_id=payment.order_id))

    assert body["id"] == payment.id
    assert body["status"] == "succeeded"
    assert body["amount_cents"] == 7_500
    assert body["computed"] == {"remaining_refundable_cents": 7_500}


async def test_get_shipment(
    shopsim: str, make_order: MakeOrder, make_shipment: MakeShipment
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    shipment = make_shipment(order, ShipmentStatus.DELAYED)

    body = data(await call("get_shipment", order_id=order.id))

    assert body["tracking_number"] == shipment.tracking_number
    assert body["status"] == "delayed"
    assert [e["location"] for e in body["events"]] == ["Lyon", "Paris"]


# --- Erreurs : message lisible, le serveur continue ------------------------------------


async def test_missing_ticket_is_a_readable_error(
    shopsim: str, make_customer: MakeCustomer, make_ticket: MakeTicket
) -> None:
    result = await call("get_ticket", ticket_id=999_999)

    assert "Ticket 999999 introuvable" in error_text(result)
    # Le serveur répond encore
    ticket = make_ticket(make_customer())
    assert data(await call("get_ticket", ticket_id=ticket.id))["id"] == ticket.id


async def test_shopsim_internal_error_is_readable(
    shopsim: str, session: Session, make_order: MakeOrder
) -> None:
    order = make_order()
    # Panne posée directement en base : le serveur MCP n'a aucun accès au Chaos
    session.add(ChaosFault(scenario=ChaosScenario.DB_ERRORS, activated_at=datetime.now(UTC)))
    session.flush()

    result = await call("get_order", order_id=order.id)

    message = error_text(result)
    assert "erreur interne (500)" in message
    assert f"GET /orders/{order.id}" in message
    # Les autres services répondent toujours
    assert data(await call("get_customer", customer_id=order.customer_id))["id"]


async def test_slow_shopsim_is_a_readable_timeout(
    shopsim: str, make_payment: MakePayment, monkeypatch: pytest.MonkeyPatch
) -> None:
    payment = make_payment()
    monkeypatch.setenv("PAYMENT_LATENCY_MS", "1000")
    monkeypatch.setenv("SHOPSIM_TIMEOUT_S", "0.2")

    result = await call("get_payment", order_id=payment.order_id)

    assert "n'a pas répondu en 0.2 s" in error_text(result)
    # Laisse ShopSim finir la requête lente avant d'annuler la transaction du test
    time.sleep(1)


async def test_unreachable_shopsim_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    monkeypatch.setenv("SHOPSIM_URL", f"http://127.0.0.1:{closed_port}")

    result = await call("get_ticket", ticket_id=1)

    assert "ShopSim est injoignable" in error_text(result)


# --- Pas d'accès au Chaos --------------------------------------------------------------


async def test_tools_never_reach_chaos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Même avec CHAOS_TOKEN dans l'environnement, aucun outil ne l'envoie."""
    monkeypatch.setenv("CHAOS_TOKEN", "secret-qui-ne-doit-pas-sortir")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        shopsim_client,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="http://shopsim.test", transport=httpx.MockTransport(handler)
        ),
    )

    for name in READ_TOOL_NAMES:
        argument = {
            "list_open_tickets": {},
            "get_ticket": {"ticket_id": 1},
            "get_order": {"order_id": 1},
            "get_payment": {"order_id": 1},
            "get_shipment": {"order_id": 1},
        }.get(name, {"customer_id": 1})
        await call(name, **argument)

    # Au moins un appel par outil (certains lisent aussi l'horloge ou les remboursements)
    assert len(requests) >= len(READ_TOOL_NAMES)
    for request in requests:
        assert request.method == "GET"
        assert "x-chaos-token" not in request.headers
        assert not request.url.path.startswith("/chaos")


# --- Valeurs calculées (le code calcule, le modèle juge) -------------------------------


async def test_shipment_computed_values(
    shopsim: str, make_order: MakeOrder, make_shipment: MakeShipment, sim_clock: SimClock
) -> None:
    delivered = make_order(OrderStatus.DELIVERED)
    make_shipment(delivered, ShipmentStatus.DELIVERED, datetime(2026, 6, 5, 15, 0, tzinfo=UTC))
    late = make_order(OrderStatus.SHIPPED)
    make_shipment(late, ShipmentStatus.DELAYED)  # livraison estimée le 2026-07-08
    sim_clock.elapse(timedelta(days=4))  # 2026-07-10 12:00

    body = data(await call("get_shipment", order_id=delivered.id))
    computed = data(await call("get_shipment", order_id=late.id))["computed"]

    assert body["computed"] == {
        "as_of": "2026-07-10T12:00:00+00:00",
        # 34 jours et 21 h : 35 jours entamés
        "days_since_delivery": 35,
        # Livraison estimée le 2026-07-08 (fabrique make_shipment)
        "days_past_estimated_delivery": 2,
    }
    assert computed["days_since_delivery"] is None
    assert computed["days_past_estimated_delivery"] == 2


async def test_shipment_not_late_yet(
    shopsim: str, make_order: MakeOrder, make_shipment: MakeShipment, sim_clock: SimClock
) -> None:
    order = make_order(OrderStatus.SHIPPED)
    make_shipment(order, ShipmentStatus.IN_TRANSIT)  # livraison estimée le 2026-07-08

    computed = data(await call("get_shipment", order_id=order.id))["computed"]

    assert computed["days_past_estimated_delivery"] == 0


async def test_customer_computed_values(
    shopsim: str, make_payment: MakePayment, make_refund: MakeRefund, sim_clock: SimClock
) -> None:
    payment = make_payment()
    for month, day in ((3, 1), (4, 10), (6, 1), (7, 1)):
        make_refund(payment, 100, datetime(2026, month, day, 12, 0, tzinfo=UTC))

    body = data(await call("get_customer", customer_id=payment.order.customer_id))

    assert body["refunds_count"] == 4
    # Fenêtre de 90 jours avant le 2026-07-06 12:00 : le 2026-03-01 en est exclu
    assert body["computed"] == {
        "as_of": "2026-07-06T12:00:00+00:00",
        "refunds_in_risk_window": 3,
        "risk_window_days": 90,
    }


async def test_payment_remaining_amount(
    shopsim: str, make_payment: MakePayment, make_refund: MakeRefund
) -> None:
    payment = make_payment(amount_cents=10_000)
    make_refund(payment, 2_500, datetime(2026, 7, 1, tzinfo=UTC))

    body = data(await call("get_payment", order_id=payment.order_id))

    assert body["computed"] == {"remaining_refundable_cents": 7_500}


async def test_computed_values_without_clock_are_null(
    shopsim: str,
    make_order: MakeOrder,
    make_shipment: MakeShipment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans horloge, l'outil renvoie les données brutes et dit pourquoi il n'a rien calculé."""
    order = make_order(OrderStatus.SHIPPED)
    make_shipment(order, ShipmentStatus.DELAYED)
    real_get_json = shopsim_client.get_json

    async def clock_down(path: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if path == "/clock":
            raise shopsim_client.ShopSimError("ShopSim a renvoyé une erreur interne (500).")
        return await real_get_json(path, *args, **kwargs)

    monkeypatch.setattr(read_tools, "get_json", clock_down)

    body = data(await call("get_shipment", order_id=order.id))

    assert body["status"] == "delayed"
    assert body["computed"] is None
    assert "Valeurs calculées indisponibles" in body["computed_error"]
