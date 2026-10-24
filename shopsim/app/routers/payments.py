"""Endpoints du service Paiements (lecture)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from shopsim.app.db import get_session
from shopsim.app.latency import simulate_payment_latency
from shopsim.app.models import Payment
from shopsim.app.schemas import PaymentRead

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/{order_id}", response_model=PaymentRead)
def get_payment(order_id: int, session: Annotated[Session, Depends(get_session)]) -> Payment:
    simulate_payment_latency()
    payment = session.scalars(select(Payment).where(Payment.order_id == order_id)).one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment
