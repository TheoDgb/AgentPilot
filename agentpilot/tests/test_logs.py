"""Trace de bout en bout et logs JSON d'AgentPilot, sans appel réel à Claude.

La trace créée pour une exécution se retrouve dans les logs d'AgentPilot, du serveur MCP (le
vrai, lancé en sous-processus) et de ShopSim (la vraie, dans un thread). Ni secret ni texte de
client n'est écrit, quel que soit le chemin (agent, hooks, page d'approbation, Chaos).
"""

import asyncio
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import decisions, sdk_runner
from agentpilot.app import app
from agentpilot.db import engine
from agentpilot.hooks import SupportHooks
from agentpilot.journal import DbJournal
from agentpilot.models import AgentKind, RunTrigger
from agentpilot.schemas import SupportDecision
from agentpilot.tests.conftest import (
    DECISION,
    TODAY,
    FakeQuery,
    HookDrivingQuery,
    MemoryJournal,
    Shop,
    call,
    init_message,
    refund,
    result_message,
    run,
)
from agentpilot.worker import Worker, new_run
from mcp_server.shopsim_client import get_json
from observability.logs import configure
from observability.trace import parse_traceparent
from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.models import MessageAuthor
from shopsim.tests.conftest import JsonLogs


@pytest.fixture
def logs(json_logs: JsonLogs) -> JsonLogs:
    """AgentPilot écrit comme en production (`agentpilot.jsonl`, logger racine), ShopSim dans
    `shopsim.jsonl`, le serveur MCP lancé par une exécution dans `mcp.jsonl` (même dossier)."""
    configure("agentpilot", json_logs.dir, stream=json_logs.stderr)
    return json_logs


def events(logs: JsonLogs, name: str, event: str) -> list[dict[str, Any]]:
    return [line for line in logs.read(name) if line["event"] == event]


def test_run_trace_reaches_shopsim_and_the_mcp_server_config(
    client: TestClient, shop: Shop, logs: JsonLogs
) -> None:
    payment = shop.payment(3_000)
    ticket = shop.ticket(payment)

    result = run(
        client, ticket, call("get_ticket", ticket_id=ticket.id), refund(ticket, payment, 3_000)
    )

    trace_id = result["trace_id"]
    assert len(trace_id) == 32
    started, finished = (
        events(logs, "agentpilot", "run.started"),
        events(logs, "agentpilot", "run.finished"),
    )
    for line in started + finished:
        assert (line["trace_id"], line["run_id"], line["ticket_id"]) == (
            trace_id,
            result["id"],
            ticket.id,
        )
    assert started[0]["trigger"] == "manual"
    assert finished[0]["status"] == "succeeded"
    assert isinstance(finished[0]["duration_ms"], int)
    # Hooks (lectures de vérification) et outils : même trace
    tools = events(logs, "agentpilot", "tool.called")
    assert [line["tool"] for line in tools] == ["get_ticket", "refund_order"]
    assert tools[1]["amount_cents"] == 3_000
    assert {line["trace_id"] for line in tools} == {trace_id}
    # Toutes les requêtes reçues par ShopSim pendant l'exécution portent cette trace
    requests = events(logs, "shopsim", "http.request")
    assert {line["route"] for line in requests} >= {"/tickets/{ticket_id}", "/refunds"}
    assert {line["trace_id"] for line in requests} == {trace_id}
    # Le serveur MCP de l'exécution reçoit la trace et le dossier des logs
    query = app.state.query
    assert isinstance(query, HookDrivingQuery) and query.options is not None
    env = query.options.mcp_servers["shopsim"]["env"]  # type: ignore[index]
    assert parse_traceparent(env["TRACEPARENT"]) == trace_id
    assert env["LOG_DIR"] == str(logs.dir)


def test_run_created_response_gives_the_trace(client: TestClient, shop: Shop) -> None:
    ticket = shop.ticket(shop.payment(3_000))
    app.state.query = HookDrivingQuery(output=DECISION | {"ticket_id": ticket.id})

    created = client.post(f"/runs/support/{ticket.id}").json()

    assert created["trace_id"] == client.get(f"/runs/{created['id']}").json()["trace_id"]


@pytest.mark.anyio
async def test_parallel_runs_keep_their_own_trace(
    shopsim: str,
    shop: Shop,
    session_factory: sessionmaker[Session],
    logs: JsonLogs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    worker = Worker(SimpleNamespace(session_factory=session_factory, query=None))
    tickets = [shop.ticket(shop.payment(3_000)) for _ in range(2)]
    runs = [
        new_run(session_factory, AgentKind.SUPPORT, t.id, RunTrigger.LOOP, check_budget=False)
        for t in tickets
    ]

    def launch(run_id: uuid.UUID, ticket_id: int) -> Any:  # noqa: ANN401
        query = HookDrivingQuery(
            *[call("get_ticket", ticket_id=ticket_id) for _ in range(3)],
            output=DECISION | {"ticket_id": ticket_id},
        )
        journal = DbJournal(session_factory, run_id)
        return lambda: sdk_runner.decide_ticket(ticket_id, journal=journal, query=query)

    await asyncio.gather(
        *(worker.execute(r, launch(r.id, t.id)) for r, t in zip(runs, tickets, strict=True))
    )

    traces = {t.id: r.trace_id for r, t in zip(runs, tickets, strict=True)}
    assert len(set(traces.values())) == 2
    finished = events(logs, "agentpilot", "run.finished")
    assert [line["status"] for line in finished] == ["succeeded", "succeeded"]
    requests = events(logs, "shopsim", "http.request")
    tools = events(logs, "agentpilot", "tool.called")
    assert len(requests) == len(tools) == 6
    for line in requests:
        ticket_id = int(line["path"].rsplit("/", 1)[1])
        assert line["trace_id"] == traces[ticket_id]
    for line in tools:
        assert line["trace_id"] == traces[line["ticket_id"]]


def test_approval_execution_has_its_own_trace(
    client: TestClient, shop: Shop, session_factory: sessionmaker[Session], logs: JsonLogs
) -> None:
    payment = shop.payment(12_000)
    ticket = shop.ticket(payment)
    result = run(client, ticket, refund(ticket, payment, 12_000))
    [pending] = result["pending_actions"]
    assert events(logs, "agentpilot", "tool.called")[0]["outcome"] == "held"

    response = client.post(
        f"/approvals/{pending['id']}/approve", data={"name": "Alice"}, follow_redirects=False
    )

    assert response.status_code == 303
    action = decisions.load(session_factory, uuid.UUID(pending["id"]))
    assert action is not None and action.trace_id is not None
    assert action.trace_id != result["trace_id"]
    (refund_request,) = [
        line for line in events(logs, "shopsim", "http.request") if line["route"] == "/refunds"
    ]
    assert refund_request["trace_id"] == action.trace_id
    (executed,) = events(logs, "agentpilot", "action.executed")
    assert (executed["trace_id"], executed["action_id"], executed["run_id"]) == (
        action.trace_id,
        pending["id"],
        result["id"],
    )
    assert (executed["tool"], executed["amount_cents"]) == ("refund_order", 12_000)
    (approved,) = events(logs, "agentpilot", "action.approved")
    assert approved["action_id"] == pending["id"]
    # L'API rend la trace de l'action
    assert client.get(f"/runs/{result['id']}").json()["pending_actions"][0]["trace_id"] == (
        action.trace_id
    )


def test_no_secret_and_no_customer_text_in_any_log(
    client: TestClient,
    shop: Shop,
    shopsim: str,
    logs: JsonLogs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "sk-ant-api03-Very-Secret-Key_0123456789"
    chaos_token = "chaos-token-never-logged-42"
    monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    monkeypatch.setenv("CHAOS_TOKEN", chaos_token)
    private = [
        "Mme Lucie Fontaine",
        "14 rue des Acacias",
        "lucie.fontaine@example.org",
        "Merci Madame Fontaine",
        "geste commercial exceptionnel",
        "Remboursement validé par Alice",
    ]
    payment = shop.payment(12_000)
    ticket = shop.make_ticket(
        payment.order.customer,
        payment.order,
        messages=[
            (
                MessageAuthor.CUSTOMER,
                f"Je suis {private[0]}, {private[1]}, écrivez-moi à {private[2]}.",
                SIMULATION_NOW,
            )
        ],
    )

    result = run(
        client,
        ticket,
        call("get_ticket", ticket_id=ticket.id),
        call("reply_to_ticket", ticket_id=ticket.id, body=f"{private[3]}, nous regardons."),
        refund(ticket, payment, 12_000, reason=private[4]),
    )
    [pending] = result["pending_actions"]
    response = client.post(
        f"/approvals/{pending['id']}/approve",
        data={
            "name": "Alice",
            "note": private[5],
            "message": f"{private[3]}, le remboursement est fait.",
            "send_message": "true",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Le Chaos aussi : jeton dans l'en-tête, jamais dans un log
    headers = {"X-Chaos-Token": chaos_token}
    assert httpx.post(f"{shopsim}/chaos/payment_latency", headers=headers).status_code == 201
    assert httpx.delete(f"{shopsim}/chaos/payment_latency", headers=headers).status_code == 204

    text = logs.everything()
    assert events(logs, "agentpilot", "action.follow_up")
    assert events(logs, "chaos", "http.request")
    for secret in (api_key, chaos_token, "sk-ant-"):
        assert secret not in text
    for words in private:
        assert words not in text
    assert "Alice" not in text
    # Une erreur SQL ne recopierait pas les valeurs non plus
    assert engine.hide_parameters is True


@pytest.mark.anyio
async def test_same_trace_from_agentpilot_through_the_real_mcp_server_to_shopsim(
    shopsim: str, shop: Shop, session_factory: sessionmaker[Session], logs: JsonLogs
) -> None:
    """Bout en bout, sans Claude : l'exécution lance le vrai serveur MCP avec la configuration
    produite par `build_options` et l'appelle comme le fait la CLI ; les hooks lisent ShopSim
    depuis AgentPilot. Même trace dans les trois fichiers."""
    ticket = shop.ticket(shop.payment(3_000))
    run_row = new_run(
        session_factory, AgentKind.SUPPORT, ticket.id, RunTrigger.MANUAL, check_budget=False
    )
    worker = Worker(SimpleNamespace(session_factory=session_factory, query=None))

    async def launch() -> sdk_runner.AgentResult:
        with tempfile.TemporaryDirectory() as workdir:
            options = sdk_runner.build_options(
                SupportDecision,
                TODAY,
                api_key="sk-ant-test",
                shopsim_url=shopsim,
                workdir=Path(workdir),
                model=sdk_runner.DEFAULT_MODEL,
                hooks=SupportHooks(ticket.id, MemoryJournal()),
                cli="/bin/true",
            )
        server = options.mcp_servers["shopsim"]  # type: ignore[index]
        params = StdioServerParameters(
            command=server["command"], args=server["args"], env=server["env"]
        )
        async with Client(params) as mcp:
            await mcp.call_tool("get_ticket", {"ticket_id": ticket.id})
            await mcp.call_tool("get_order", {"order_id": ticket.order_id})
        # Lecture de vérification d'un hook, depuis AgentPilot
        await get_json(f"/tickets/{ticket.id}", "introuvable")
        return sdk_runner.AgentResult(output={})

    await worker.execute(run_row, launch)

    trace_id = run_row.trace_id
    agentpilot, mcp, shopsim_lines = logs.read("agentpilot"), logs.read("mcp"), logs.read("shopsim")
    assert [line["event"] for line in agentpilot] == ["run.started", "shopsim.call", "run.finished"]
    assert [line["event"] for line in mcp] == [
        "mcp.started",
        "shopsim.call",
        "tool.call",
        "shopsim.call",
        "tool.call",
    ]
    assert [line["route"] for line in shopsim_lines] == [
        "/tickets/{ticket_id}",
        "/orders/{order_id}",
        "/tickets/{ticket_id}",
    ]
    for line in agentpilot + mcp + shopsim_lines:
        assert line["trace_id"] == trace_id
    assert {line["service"] for line in agentpilot + mcp + shopsim_lines} == {
        "agentpilot",
        "mcp",
        "shopsim",
    }


def test_run_cost_is_rounded_in_logs(client: TestClient, shop: Shop, logs: JsonLogs) -> None:
    """Le coût du SDK est un flottant (0.024513300000000002) : arrondi comme en base."""
    ticket = shop.ticket(shop.payment(3_000))
    app.state.query = FakeQuery(
        init_message(),
        result_message(DECISION | {"ticket_id": ticket.id}, total_cost_usd=0.024513300000000002),
    )

    client.post(f"/runs/support/{ticket.id}")

    (finished,) = events(logs, "agentpilot", "run.finished")
    assert finished["cost_usd"] == 0.024513
