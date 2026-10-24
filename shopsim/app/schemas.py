"""Schémas Pydantic des requêtes et réponses de l'API."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from shopsim.app.models import OrderStatus, PaymentStatus, ShipmentStatus


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
