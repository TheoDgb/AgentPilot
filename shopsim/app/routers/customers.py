"""Endpoints du service Clients (lecture)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.clock import SIMULATION_NOW
from shopsim.app.db import get_session
from shopsim.app.models import Customer, Order, Payment, Refund, Ticket, TicketStatus
from shopsim.app.schemas import CustomerRead, OrderRead

router = APIRouter(prefix="/customers", tags=["customers"])


def _get_customer(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.get("/{customer_id}", response_model=CustomerRead)
def get_customer(
    customer_id: int, session: Annotated[Session, Depends(get_session)]
) -> CustomerRead:
    customer = _get_customer(session, customer_id)
    resolved_tickets = session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(Ticket.customer_id == customer_id, Ticket.status == TicketStatus.RESOLVED)
    )
    refunds = session.scalar(
        select(func.count())
        .select_from(Refund)
        .join(Payment, Refund.payment_id == Payment.id)
        .join(Order, Payment.order_id == Order.id)
        .where(Order.customer_id == customer_id)
    )
    return CustomerRead(
        id=customer.id,
        email=customer.email,
        first_name=customer.first_name,
        last_name=customer.last_name,
        created_at=customer.created_at,
        tenure_days=(SIMULATION_NOW - customer.created_at).days,
        resolved_tickets_count=resolved_tickets or 0,
        refunds_count=refunds or 0,
    )


@router.get("/{customer_id}/orders", response_model=list[OrderRead])
def list_customer_orders(
    customer_id: int, session: Annotated[Session, Depends(get_session)]
) -> list[Order]:
    _get_customer(session, customer_id)
    # Les plus récentes d'abord
    return list(
        session.scalars(
            select(Order)
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
        )
    )
