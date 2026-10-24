"""Endpoints du service Commandes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from shopsim.app.db import get_session
from shopsim.app.models import Order
from shopsim.app.schemas import OrderRead

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}", response_model=OrderRead)
def get_order(order_id: int, session: Annotated[Session, Depends(get_session)]) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
