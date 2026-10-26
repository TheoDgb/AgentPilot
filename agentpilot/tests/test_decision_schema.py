"""Schéma de décision de l'agent Support."""

from typing import Any

import pytest
from pydantic import ValidationError

from agentpilot.schemas import SupportDecision


def decision(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "ticket_id": 4,
        "category": "delivery_delay",
        "action": "reply",
        "requires_human_approval": False,
        "customer_reply": "Bonjour, votre colis est en route.\n\nLe service client Lumen",
        "facts_used": [{"tool": "get_shipment", "field": "status", "value": "delayed"}],
    }
    return base | changes


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"action": "refund", "amount_cents": 3420},
        {"action": "resend", "amount_cents": 15194, "requires_human_approval": True},
        {"action": "refuse"},
        {"action": "escalate", "escalation_reason": "Commande d'un autre client"},
        {"facts_used": []},
    ],
)
def test_valid_decisions(changes: dict[str, Any]) -> None:
    assert SupportDecision.model_validate(decision(**changes)).ticket_id == 4


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"action": "refund"}, "amount_cents est obligatoire"),
        ({"action": "resend"}, "amount_cents est obligatoire"),
        ({"amount_cents": 1000}, "amount_cents doit être vide"),
        ({"action": "refund", "amount_cents": 0}, "greater than 0"),
        ({"action": "escalate"}, "escalation_reason est obligatoire"),
        ({"escalation_reason": "Doute"}, "escalation_reason doit être vide"),
        ({"category": "other"}, "category"),
        ({"action": "close"}, "action"),
        ({"customer_reply": ""}, "customer_reply"),
        ({"confidence": 0.9}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_decisions(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SupportDecision.model_validate(decision(**changes))


def test_json_schema_lists_categories_and_actions() -> None:
    schema = SupportDecision.model_json_schema()

    assert "trap_prompt_injection" in schema["$defs"]["TicketCategory"]["enum"]
    assert schema["$defs"]["ExpectedAction"]["enum"] == [
        "reply",
        "refund",
        "resend",
        "refuse",
        "escalate",
    ]
    assert schema["additionalProperties"] is False
