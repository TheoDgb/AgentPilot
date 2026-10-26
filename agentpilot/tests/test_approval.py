"""Décision de validation humaine (`agentpilot/approval.py`), sans réseau."""

from datetime import timedelta

import pytest

from agentpilot.approval import (
    ESCALATE,
    REFUND,
    REPLY,
    RESEND,
    RESOLVE,
    Kind,
    OrderFacts,
    Reason,
    Verdict,
    evaluate,
    hold_message,
    idempotency_key,
)
from evals.policy import APPROVAL_THRESHOLD_CENTS, RISK_MIN_REFUNDS, RISK_WINDOW
from mcp_server import idempotency
from shopsim.app.clock import SIMULATION_NOW

NOW = SIMULATION_NOW
TICKET = 7


def facts(
    remaining: int | None = 3_000,
    total: int = 3_000,
    refund_days_ago: tuple[int, ...] = (),
    order_customer: int = 1,
) -> OrderFacts:
    return OrderFacts(
        ticket_customer_id=1,
        order_customer_id=order_customer,
        order_total_cents=total,
        remaining_refundable_cents=remaining,
        refund_dates=tuple(NOW - timedelta(days=d) for d in refund_days_ago),
        now=NOW,
    )


def refund_input(amount: int, ticket_id: int = TICKET) -> dict[str, object]:
    return {"ticket_id": ticket_id, "order_id": 40, "amount_cents": amount, "reason": "x"}


def test_threshold_comes_from_policy() -> None:
    assert APPROVAL_THRESHOLD_CENTS == 5_000


@pytest.mark.parametrize(("amount", "kind"), [(5_000, Kind.ALLOW), (5_001, Kind.HOLD)])
def test_refund_threshold_is_exact(amount: int, kind: Kind) -> None:
    verdict = evaluate(
        REFUND, refund_input(amount), run_ticket_id=TICKET, facts=facts(amount, amount)
    )

    assert verdict.kind is kind
    assert verdict.value_cents == amount


def test_understated_refund_uses_the_remaining_amount() -> None:
    verdict = evaluate(
        REFUND, refund_input(3_000), run_ticket_id=TICKET, facts=facts(12_000, 12_000)
    )

    assert verdict.kind is Kind.HOLD
    assert verdict.reasons == (Reason.OVER_THRESHOLD,)
    assert verdict.value_cents == 12_000


def test_requested_amount_can_only_raise_the_value() -> None:
    verdict = evaluate(REFUND, refund_input(9_000), run_ticket_id=TICKET, facts=facts(2_000))

    assert verdict.kind is Kind.HOLD
    assert verdict.value_cents == 9_000


@pytest.mark.parametrize(
    ("days", "kind"),
    [
        ((10,) * (RISK_MIN_REFUNDS - 1), Kind.ALLOW),
        ((10,) * RISK_MIN_REFUNDS, Kind.HOLD),
        # Hors de la fenêtre de risque : ne compte pas
        ((10, 20, RISK_WINDOW.days + 1), Kind.ALLOW),
    ],
)
def test_risky_customer(days: tuple[int, ...], kind: Kind) -> None:
    verdict = evaluate(
        REFUND, refund_input(1_000), run_ticket_id=TICKET, facts=facts(refund_days_ago=days)
    )

    assert verdict.kind is kind
    if kind is Kind.HOLD:
        assert verdict.reasons == (Reason.RISKY_CUSTOMER,)


def test_both_reasons() -> None:
    verdict = evaluate(
        REFUND,
        refund_input(9_000),
        run_ticket_id=TICKET,
        facts=facts(9_000, 9_000, refund_days_ago=(1, 2, 3)),
    )

    assert verdict.reasons == (Reason.OVER_THRESHOLD, Reason.RISKY_CUSTOMER)


@pytest.mark.parametrize(("total", "kind"), [(5_000, Kind.ALLOW), (15_000, Kind.HOLD)])
def test_resend_uses_the_order_value(total: int, kind: Kind) -> None:
    verdict = evaluate(
        RESEND,
        {"ticket_id": TICKET, "order_id": 40, "reason": "x"},
        run_ticket_id=TICKET,
        facts=facts(None, total),
    )

    assert verdict.kind is kind
    assert verdict.value_cents == total


def test_order_of_another_customer_is_denied() -> None:
    verdict = evaluate(
        REFUND, refund_input(1_000), run_ticket_id=TICKET, facts=facts(order_customer=2)
    )

    assert verdict.kind is Kind.DENY
    assert "n'appartient pas au client du ticket 7" in verdict.message


@pytest.mark.parametrize("tool", [REPLY, ESCALATE, RESOLVE, REFUND])
def test_other_ticket_is_denied(tool: str) -> None:
    verdict = evaluate(tool, refund_input(1_000, ticket_id=8), run_ticket_id=TICKET, facts=facts())

    assert verdict.kind is Kind.DENY
    assert "aucune action sur un autre ticket" in verdict.message


def test_no_action_during_a_check() -> None:
    verdict = evaluate(REPLY, {"ticket_id": TICKET, "body": "x"}, run_ticket_id=None)

    assert verdict.kind is Kind.DENY


@pytest.mark.parametrize("tool", [REPLY, ESCALATE])
def test_reply_and_escalation_pass(tool: str) -> None:
    verdict = evaluate(tool, {"ticket_id": TICKET}, run_ticket_id=TICKET, pending_on_ticket=True)

    assert verdict.kind is Kind.ALLOW


def test_resolve_waits_for_pending_actions() -> None:
    assert evaluate(RESOLVE, {"ticket_id": TICKET}, run_ticket_id=TICKET).kind is Kind.ALLOW
    blocked = evaluate(RESOLVE, {"ticket_id": TICKET}, run_ticket_id=TICKET, pending_on_ticket=True)
    assert blocked.kind is Kind.DENY


def test_unknown_tool_is_denied() -> None:
    verdict = evaluate("delete_order", {"ticket_id": TICKET}, run_ticket_id=TICKET)

    assert verdict.kind is Kind.DENY


def test_ticket_id_must_be_an_integer() -> None:
    verdict = evaluate(REPLY, {"ticket_id": str(TICKET)}, run_ticket_id=TICKET)

    assert verdict.kind is Kind.DENY


def test_idempotency_key_is_the_mcp_key() -> None:
    assert idempotency_key(REFUND, refund_input(1_000)) == idempotency.refund_key(TICKET, 40)
    assert idempotency_key(RESEND, {"ticket_id": TICKET, "order_id": 40}) == (
        idempotency.resend_key(TICKET, 40)
    )


def test_hold_message_keeps_the_reason_from_the_customer() -> None:
    verdict = Verdict(Kind.HOLD, reasons=(Reason.OVER_THRESHOLD,), value_cents=9_000)

    message = hold_message(verdict, already_pending=False)

    assert "transmise pour validation, sans lui en donner la raison" in message
    assert "N'annonce pas au client qu'elle est faite" in message
