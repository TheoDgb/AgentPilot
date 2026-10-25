"""Schéma de la vérité terrain des tickets (`evals/tickets.jsonl`).

Ces informations ne sont jamais stockées en base ni exposées par l'API ShopSim :
seuls le seed (qui les écrit) et l'évaluation (qui les lit) les connaissent.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TicketCategory(StrEnum):
    DELIVERY_DELAY = "delivery_delay"
    PAYMENT_NOT_CONFIRMED = "payment_not_confirmed"
    REFUND_REQUEST = "refund_request"
    DEFECTIVE_PRODUCT = "defective_product"
    SIMPLE_QUESTION = "simple_question"
    TRAP_OTHER_CUSTOMER_ORDER = "trap_other_customer_order"
    TRAP_PROMPT_INJECTION = "trap_prompt_injection"
    TRAP_AMOUNT_OVER_THRESHOLD = "trap_amount_over_threshold"


TRAP_CATEGORIES = frozenset(
    {
        TicketCategory.TRAP_OTHER_CUSTOMER_ORDER,
        TicketCategory.TRAP_PROMPT_INJECTION,
        TicketCategory.TRAP_AMOUNT_OVER_THRESHOLD,
    }
)


class ExpectedAction(StrEnum):
    REPLY = "reply"
    REFUND = "refund"
    RESEND = "resend"
    # Réponse de refus poli : distinguée de `reply` pour l'évaluation
    REFUSE = "refuse"
    ESCALATE = "escalate"


class TicketGroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: int
    category: TicketCategory
    expected_action: ExpectedAction
    requires_human_approval: bool
    # Montant remboursé ou valeur de l'article renvoyé
    action_value_cents: int | None = None
    # Faits que la réponse doit mentionner
    facts: dict[str, str | int | bool] = Field(default_factory=dict)
    # Données d'un autre client, à ne jamais divulguer
    forbidden_facts: dict[str, str] = Field(default_factory=dict)
