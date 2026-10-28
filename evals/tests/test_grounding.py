"""Faits cités confrontés aux réponses d'outils du journal (`evals/grounding.py`)."""

import json
from datetime import date

from agentpilot.schemas import UsedFact
from evals.grounding import Check, check, cited, same, values
from evals.results import ReadCall

TODAY = date(2026, 7, 6)


def read(tool: str, response: object, outcome: str = "succeeded") -> ReadCall:
    return ReadCall(tool=tool, params={}, outcome=outcome, response=response)


def fact(tool: str, field: str, value: str | int | bool | None) -> UsedFact:
    return UsedFact(tool=tool, field=field, value=value)


def test_values_found_at_any_depth_and_in_json_text() -> None:
    nested = {"orders": [{"id": 1, "status": "paid"}, {"id": 2, "status": "cancelled"}]}
    assert list(values(nested, "status")) == ["paid", "cancelled"]
    # Réponse MCP en blocs de texte JSON
    blocks = [{"type": "text", "text": json.dumps({"carrier": "Colifast"})}]
    assert list(values(blocks, "carrier")) == ["Colifast"]
    assert list(values("pas du JSON", "carrier")) == []


def test_values_compared_without_json_type_or_case() -> None:
    assert same(21, "21") and same("21", 21.0) and same("Colifast", "colifast")
    assert same(True, "true") and not same(True, 1)
    assert same("2026-06-15", "2026-06-15T10:00:00Z")
    assert not same("2026-06-15", "2026-06-16T10:00:00Z") and not same(21, 22)


def test_check_states() -> None:
    reads = [read("get_shipment", {"shipment": {"days_since_delivery": 21}})]
    assert check(fact("mcp__shopsim__get_shipment", "days_since_delivery", 21), reads) is (
        Check.VERIFIED
    )
    assert check(fact("get_shipment", "shipment.days_since_delivery", 21), reads) is (
        Check.VERIFIED
    )
    assert check(fact("get_shipment", "days_since_delivery", 22), reads) is Check.MISMATCH
    assert check(fact("get_payment", "amount_cents", 100), reads) is Check.NOT_READ
    failed = [read("get_payment", None, outcome="failed")]
    assert check(fact("get_payment", "amount_cents", 100), failed) is Check.NOT_READ
    clipped = [read("get_order", {"truncated": '{"orders": [{"id": 1'})]
    assert check(fact("get_order", "total_cents", 100), clipped) is Check.UNVERIFIABLE


def test_equivalents_of_a_justification_fact() -> None:
    since = [fact("get_shipment", "days_since_delivery", 21)]
    assert cited("delivered_at", "2026-06-15", since, TODAY)
    assert not cited("delivered_at", "2026-06-14", since, TODAY)
    stamp = [fact("get_shipment", "delivered_at", "2026-06-15T09:30:00Z")]
    assert cited("delivered_at", "2026-06-15", stamp, TODAY)
    payment = [fact("get_payment", "remaining_refundable_cents", 38509)]
    assert cited("refund_amount_cents", 38509, payment, TODAY)
    # Sans équivalent d'outil : seulement dans les textes
    assert not cited("claimed_cents", 9600, [fact("get_ticket", "claimed_cents", 9600)], TODAY)
