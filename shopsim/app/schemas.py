"""Schémas Pydantic des requêtes et réponses de l'API."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from shopsim.app.models import (
    ChaosScenario,
    MessageAuthor,
    OrderStatus,
    PaymentStatus,
    ShipmentStatus,
    TicketStatus,
)


class CustomerRead(BaseModel):
    id: int
    email: str
    first_name: str
    last_name: str
    created_at: datetime
    # Ancienneté en jours à la date de référence de la simulation
    tenure_days: int
    # Tickets résolus : le ticket en cours de traitement n'est pas compté
    resolved_tickets_count: int
    # Remboursements reçus, toutes commandes confondues
    refunds_count: int


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    status: OrderStatus
    total_cents: int
    currency: str
    created_at: datetime


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    status: PaymentStatus
    amount_cents: int
    refunded_cents: int
    created_at: datetime


class RefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: int
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class RefundRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_id: int
    amount_cents: int
    reason: str
    idempotency_key: str
    created_at: datetime


class CustomerRefundRead(BaseModel):
    """Remboursement vu depuis le client : sert à la règle « client à risque »."""

    id: int
    order_id: int
    amount_cents: int
    reason: str
    created_at: datetime


class ShipmentEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    occurred_at: datetime
    location: str
    description: str


class ShipmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    carrier: str
    tracking_number: str
    status: ShipmentStatus
    estimated_delivery: date
    delivered_at: datetime | None
    created_at: datetime
    events: list[ShipmentEventRead]


class TicketSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    order_id: int | None
    subject: str
    status: TicketStatus
    created_at: datetime


class TicketMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    author: MessageAuthor
    body: str
    created_at: datetime


class TicketRead(TicketSummary):
    escalation_reason: str | None
    messages: list[TicketMessageRead]


class TicketReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=5000)


class TicketEscalate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class TicketPage(BaseModel):
    items: list[TicketSummary]
    total: int
    limit: int
    offset: int


class ChaosFaultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scenario: ChaosScenario
    activated_at: datetime


class ChaosState(BaseModel):
    active: list[ChaosFaultRead]
