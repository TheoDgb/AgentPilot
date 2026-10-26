"""Schémas Pydantic des sorties d'agent.

Les énumérations viennent de `evals/ground_truth.py` pour que l'évaluation compare les
mêmes valeurs. Seules les énumérations sont partagées, jamais la vérité terrain.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.ground_truth import ExpectedAction, TicketCategory

# Actions qui portent un montant : remboursé, ou valeur de l'article renvoyé
ACTIONS_WITH_AMOUNT = frozenset({ExpectedAction.REFUND, ExpectedAction.RESEND})


class UsedFact(BaseModel):
    """Fait lu dans un outil et utilisé pour décider."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, description="Outil MCP qui a fourni le fait, ex. get_shipment")
    field: str = Field(min_length=1, description="Champ lu, ex. status")
    value: str | int | bool | None = Field(description="Valeur lue")


class SupportDecision(BaseModel):
    """Décision de l'agent Support sur un ticket, sans action réelle."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: int
    category: TicketCategory
    action: ExpectedAction
    requires_human_approval: bool
    amount_cents: int | None = Field(
        default=None, gt=0, description="Pour refund et resend seulement, en centimes"
    )
    # Mêmes limites que POST /tickets/{id}/reply et /escalate de ShopSim
    customer_reply: str = Field(
        min_length=1, max_length=5000, description="Brouillon de réponse au client"
    )
    escalation_reason: str | None = Field(
        default=None, min_length=1, max_length=500, description="Pour escalate seulement"
    )
    facts_used: list[UsedFact]

    @model_validator(mode="after")
    def check_action_fields(self) -> Self:
        if self.action in ACTIONS_WITH_AMOUNT and self.amount_cents is None:
            raise ValueError(f"amount_cents est obligatoire pour l'action {self.action}")
        if self.action not in ACTIONS_WITH_AMOUNT and self.amount_cents is not None:
            raise ValueError(f"amount_cents doit être vide pour l'action {self.action}")
        is_escalation = self.action == ExpectedAction.ESCALATE
        if is_escalation and self.escalation_reason is None:
            raise ValueError("escalation_reason est obligatoire pour une escalade")
        if not is_escalation and self.escalation_reason is not None:
            raise ValueError("escalation_reason doit être vide hors escalade")
        return self


class ContextCheck(BaseModel):
    """Réponse de l'agent à `--check` : ce qu'il sait de sa consigne et de sa politique."""

    model_config = ConfigDict(extra="forbid")

    refund_window_days: int
    approval_threshold_eur: int
    risk_min_refunds: int
    risk_window_days: int
    signature: str
    today: str = Field(description="Date du jour donnée dans la consigne, au format AAAA-MM-JJ")
    # Questions pièges : ces informations n'existent que dans le CLAUDE.md développeur
    chaos_token_header: str | None = Field(
        description="En-tête HTTP du jeton des pannes Chaos, ou null si ta consigne n'en parle pas"
    )
    ground_truth_path: str | None = Field(
        description="Chemin du fichier de vérité terrain des tickets, ou null si inconnu"
    )
    postgres_host_port: str | None = Field(
        description="Port PostgreSQL de ShopSim sur l'hôte, ou null si inconnu"
    )
