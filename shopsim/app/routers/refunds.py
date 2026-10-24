"""Endpoint de remboursement idempotent (service Paiements)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopsim.app.db import get_session
from shopsim.app.latency import simulate_payment_latency
from shopsim.app.models import OrderStatus, Payment, PaymentStatus, Refund
from shopsim.app.schemas import RefundCreate, RefundRead

router = APIRouter(prefix="/refunds", tags=["refunds"])


def _find_by_key(session: Session, key: str) -> Refund | None:
    return session.scalars(select(Refund).where(Refund.idempotency_key == key)).one_or_none()


def _replay(existing: Refund, body: RefundCreate) -> Refund:
    """Rejeu d'une clé connue : même contenu → remboursement existant, sinon 409."""
    same_request = (
        existing.payment_id == body.payment_id
        and existing.amount_cents == body.amount_cents
        and existing.reason == body.reason
    )
    if not same_request:
        raise HTTPException(
            status_code=409, detail="Idempotency-Key already used with a different request"
        )
    return existing


@router.post(
    "",
    response_model=RefundRead,
    status_code=201,
    responses={
        200: {"description": "Rejeu : remboursement existant pour cette clé"},
        400: {"description": "En-tête Idempotency-Key manquant"},
        404: {"description": "Paiement inexistant"},
        409: {"description": "Clé réutilisée avec un autre contenu, ou paiement non remboursable"},
        422: {"description": "Montant supérieur au reste remboursable"},
    },
)
def create_refund(
    body: RefundCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> Refund:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    simulate_payment_latency()

    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(existing, body)

    # Verrou sur le paiement : les remboursements d'un même paiement sont sérialisés
    payment = session.scalars(
        select(Payment).where(Payment.id == body.payment_id).with_for_update()
    ).one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Une requête concurrente avec la même clé a pu aboutir pendant l'attente du verrou
    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(existing, body)

    if payment.status != PaymentStatus.SUCCEEDED:
        raise HTTPException(status_code=409, detail="Payment is not refundable")

    already_refunded = session.scalar(
        select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
            Refund.payment_id == payment.id
        )
    )
    remaining = payment.amount_cents - already_refunded
    if body.amount_cents > remaining:
        raise HTTPException(
            status_code=422,
            detail={"message": "Refund exceeds remaining amount", "remaining_cents": remaining},
        )

    refund = Refund(
        payment_id=payment.id,
        amount_cents=body.amount_cents,
        reason=body.reason,
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    try:
        with session.begin_nested():
            session.add(refund)
    except IntegrityError:
        # Même clé insérée entre-temps par une requête portant sur un autre paiement
        existing = _find_by_key(session, idempotency_key)
        if existing is None:
            raise
        response.status_code = 200
        return _replay(existing, body)

    if body.amount_cents == remaining:
        payment.status = PaymentStatus.REFUNDED
        payment.order.status = OrderStatus.REFUNDED

    session.commit()
    return refund
