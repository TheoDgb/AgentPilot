"""Endpoints du service Tickets (lecture)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from shopsim.app.db import get_session
from shopsim.app.models import Ticket, TicketStatus
from shopsim.app.schemas import TicketPage, TicketRead, TicketSummary

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.get("", response_model=TicketPage)
def list_tickets(
    session: Annotated[Session, Depends(get_session)],
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketPage:
    query = select(Ticket)
    if status is not None:
        query = query.where(Ticket.status == status)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    # Les plus anciens d'abord ; l'id départage pour un ordre stable entre les pages
    tickets = session.scalars(
        query.order_by(Ticket.created_at, Ticket.id).limit(limit).offset(offset)
    )
    return TicketPage(
        items=[TicketSummary.model_validate(t) for t in tickets],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{ticket_id}", response_model=TicketRead)
def get_ticket(ticket_id: int, session: Annotated[Session, Depends(get_session)]) -> Ticket:
    ticket = session.scalars(
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages))
    ).one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket
