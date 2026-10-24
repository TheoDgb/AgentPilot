"""Endpoints du service Livraisons."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shopsim.app.db import get_session
from shopsim.app.models import Shipment
from shopsim.app.schemas import ShipmentRead

router = APIRouter(prefix="/shipments", tags=["shipments"])


@router.get("/{order_id}", response_model=ShipmentRead)
def get_shipment(order_id: int, session: Annotated[Session, Depends(get_session)]) -> Shipment:
    shipment = session.scalars(
        select(Shipment).where(Shipment.order_id == order_id).options(selectinload(Shipment.events))
    ).one_or_none()
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return shipment
