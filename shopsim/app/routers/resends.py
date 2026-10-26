"""Endpoint de renvoi idempotent (service Commandes, ajout validé après J1).

Une commande n'a qu'une livraison : un renvoi crée donc une commande de remplacement,
gratuite pour le client, qui porte la nouvelle livraison.
"""

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopsim.app import clock
from shopsim.app.chaos import apply_connection_saturation
from shopsim.app.db import get_session
from shopsim.app.models import (
    Order,
    OrderStatus,
    Resend,
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
)
from shopsim.app.schemas import ResendCreate, ResendRead

# Panne Chaos connection_saturation : s'applique à toutes les routes métier
router = APIRouter(
    prefix="/orders", tags=["orders"], dependencies=[Depends(apply_connection_saturation)]
)

# Seules ces livraisons justifient un renvoi : article reçu (défectueux) ou colis perdu
RESENDABLE = frozenset({ShipmentStatus.DELIVERED, ShipmentStatus.LOST})
RESEND_DELIVERY_DELAY = timedelta(days=3)
WAREHOUSE = "Entrepôt Lumen, Orléans"


def _find_by_key(session: Session, key: str) -> Resend | None:
    return session.scalars(select(Resend).where(Resend.idempotency_key == key)).one_or_none()


def _read(resend: Resend) -> ResendRead:
    shipment = resend.replacement_order.shipment
    assert shipment is not None
    return ResendRead(
        id=resend.id,
        order_id=resend.order_id,
        replacement_order_id=resend.replacement_order_id,
        carrier=shipment.carrier,
        tracking_number=shipment.tracking_number,
        reason=resend.reason,
        idempotency_key=resend.idempotency_key,
        created_at=resend.created_at,
    )


def _replay(existing: Resend, order_id: int, body: ResendCreate) -> ResendRead:
    """Rejeu d'une clé connue : même contenu → renvoi existant, sinon 409."""
    if existing.order_id != order_id or existing.reason != body.reason:
        raise HTTPException(
            status_code=409, detail="Idempotency-Key already used with a different request"
        )
    return _read(existing)


def _new_tracking_number(session: Session, prefix: str) -> str:
    while True:
        candidate = f"{prefix}{secrets.randbelow(10**10):010d}"
        taken = session.scalar(select(Shipment.id).where(Shipment.tracking_number == candidate))
        if taken is None:
            return candidate


@router.post(
    "/{order_id}/resend",
    response_model=ResendRead,
    status_code=201,
    responses={
        200: {"description": "Rejeu : renvoi existant pour cette clé"},
        400: {"description": "En-tête Idempotency-Key manquant"},
        404: {"description": "Commande inexistante"},
        409: {
            "description": "Clé réutilisée avec un autre contenu, commande non renvoyable "
            "ou déjà renvoyée"
        },
    },
)
def resend_order(
    order_id: int,
    body: ResendCreate,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> ResendRead:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    # Le rejeu passe avant le contrôle d'état : un renvoi déjà créé reste un succès
    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(existing, order_id, body)

    # Verrou sur la commande : les renvois d'une même commande sont sérialisés
    order = session.scalars(
        select(Order).where(Order.id == order_id).with_for_update()
    ).one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    # Une requête concurrente avec la même clé a pu aboutir pendant l'attente du verrou
    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _replay(existing, order_id, body)

    original = order.shipment
    if original is None or original.status not in RESENDABLE:
        raise HTTPException(status_code=409, detail="Order cannot be resent")
    if session.scalar(select(Resend.id).where(Resend.order_id == order_id)) is not None:
        raise HTTPException(status_code=409, detail="Order already resent")

    now = clock.now(session)
    replacement = Order(
        customer_id=order.customer_id,
        status=OrderStatus.SHIPPED,
        total_cents=0,
        currency=order.currency,
        created_at=now,
    )
    shipment = Shipment(
        order=replacement,
        carrier=original.carrier,
        # Même préfixe que le transporteur d'origine (RPD, EXP, CLF)
        tracking_number=_new_tracking_number(session, original.tracking_number[:3]),
        status=ShipmentStatus.LABEL_CREATED,
        estimated_delivery=(now + RESEND_DELIVERY_DELAY).date(),
        delivered_at=None,
        created_at=now,
        events=[ShipmentEvent(occurred_at=now, location=WAREHOUSE, description="Étiquette créée")],
    )
    resend = Resend(
        order_id=order.id,
        replacement_order=replacement,
        reason=body.reason,
        idempotency_key=idempotency_key,
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add_all([replacement, shipment, resend])
    except IntegrityError:
        # Même clé insérée entre-temps par une requête portant sur une autre commande
        existing = _find_by_key(session, idempotency_key)
        if existing is None:
            raise
        response.status_code = 200
        return _replay(existing, order_id, body)

    session.commit()
    return _read(resend)
