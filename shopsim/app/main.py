"""Application FastAPI ShopSim."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shopsim.app.db import init_db
from shopsim.app.routers import orders


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="ShopSim", lifespan=lifespan)
app.include_router(orders.router)
