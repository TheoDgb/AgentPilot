"""Endpoint de santé (utilisé par le healthcheck Docker)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from shopsim.app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
def health(response: Response, session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}
