"""Schémas Pydantic des réponses de l'API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from shopsim.app.models import OrderStatus


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    status: OrderStatus
    total_cents: int
    currency: str
    created_at: datetime
