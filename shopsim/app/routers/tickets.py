"""Endpoints du service Tickets : lecture, réponse idempotente, escalade et résolution."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from shopsim.app import clock
from shopsim.app.chaos import apply_connection_saturation
from shopsim.app.db import get_session
from shopsim.app.models import MessageAuthor, Ticket, TicketMessage, TicketStatus
from shopsim.app.schemas import (
    TicketEscalate,
    TicketPage,
    TicketRead,
    TicketReplyCreate,
    TicketSummary,
)

# Panne Chaos connection_saturation : s'applique à toutes les routes métier
router = APIRouter(
    prefix="/tickets", tags=["tickets"], dependencies=[Depends(apply_connection_saturation)]
)


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


def _get_ticket(session: Session, ticket_id: int) -> Ticket:
    ticket = session.scalars(
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages))
    ).one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _lock_ticket(session: Session, ticket_id: int) -> Ticket:
    """Verrou sur le ticket : les actions sur un même ticket sont sérialisées."""
    ticket = session.scalars(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()
    ).one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def _reject_if_resolved(ticket: Ticket) -> None:
    if ticket.status == TicketStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="Ticket is resolved")


@router.get("/{ticket_id}", response_model=TicketRead)
def get_ticket(ticket_id: int, session: Annotated[Session, Depends(get_session)]) -> Ticket:
    return _get_ticket(session, ticket_id)


def _find_by_key(session: Session, key: str) -> TicketMessage | None:
    return session.scalars(
        select(TicketMessage).where(TicketMessage.idempotency_key == key)
    ).one_or_none()


def _replay(session: Session, existing: TicketMessage, ticket_id: int, body: str) -> Ticket:
    """Rejeu d'une clé connue : même contenu → ticket tel quel, sinon 409."""
    if existing.ticket_id != ticket_id or existing.body != body:
        raise HTTPException(
            status_code=409, detail="Idempotency-Key already used with a different request"
        )
    return _get_ticket(session, ticket_id)


@router.post(
    "/{ticket_id}/reply",
    response_model=TicketRead,
    status_code=201,
    responses={
        200: {"description": "Rejeu : réponse déjà créée pour cette clé"},
        400: {"description": "En-tête Idempotency-Key manquant"},
        404: {"description": "Ticket inexistant"},
        409: {"description": "Clé réutilisée avec un autre contenu, ou ticket résolu"},
    },
)
def reply_to_ticket(
    ticket_id: int,
    reply: TicketReplyCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> Ticket:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    # Le rejeu passe avant le contrôle d'état : une réponse déjà créée reste un succès
    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(session, existing, ticket_id, reply.body)

    ticket = _lock_ticket(session, ticket_id)

    # Une requête concurrente avec la même clé a pu aboutir pendant l'attente du verrou
    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(session, existing, ticket_id, reply.body)

    _reject_if_resolved(ticket)

    message = TicketMessage(
        ticket_id=ticket.id,
        author=MessageAuthor.AGENT,
        body=reply.body,
        idempotency_key=idempotency_key,
        created_at=clock.now(session),
    )
    try:
        with session.begin_nested():
            session.add(message)
    except IntegrityError:
        # Même clé insérée entre-temps par une requête portant sur un autre ticket
        existing = _find_by_key(session, idempotency_key)
        if existing is None:
            raise
        response.status_code = 200
        return _replay(session, existing, ticket_id, reply.body)

    session.commit()
    return _get_ticket(session, ticket_id)


@router.post(
    "/{ticket_id}/escalate",
    response_model=TicketRead,
    responses={
        404: {"description": "Ticket inexistant"},
        409: {"description": "Ticket résolu, ou déjà escaladé pour un autre motif"},
    },
)
def escalate_ticket(
    ticket_id: int,
    escalation: TicketEscalate,
    session: Annotated[Session, Depends(get_session)],
) -> Ticket:
    ticket = _lock_ticket(session, ticket_id)
    _reject_if_resolved(ticket)
    if ticket.status == TicketStatus.ESCALATED:
        # Rejeu de la même escalade : succès sans effet ; un autre motif n'écrase pas le premier
        if ticket.escalation_reason != escalation.reason:
            raise HTTPException(
                status_code=409, detail="Ticket already escalated with a different reason"
            )
    else:
        ticket.status = TicketStatus.ESCALATED
        ticket.escalation_reason = escalation.reason
    session.commit()
    return _get_ticket(session, ticket_id)


@router.post(
    "/{ticket_id}/resolve",
    response_model=TicketRead,
    responses={404: {"description": "Ticket inexistant"}},
)
def resolve_ticket(ticket_id: int, session: Annotated[Session, Depends(get_session)]) -> Ticket:
    ticket = _lock_ticket(session, ticket_id)
    # Déjà résolu : succès sans effet, pour qu'un réessai après une coupure ne soit pas une erreur.
    # Le motif d'escalade éventuel est conservé pour l'historique.
    ticket.status = TicketStatus.RESOLVED
    session.commit()
    return _get_ticket(session, ticket_id)
