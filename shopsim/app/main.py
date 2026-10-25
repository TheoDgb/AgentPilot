"""Application FastAPI ShopSim."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shopsim.app.db import init_db
from shopsim.app.routers import customers, health, orders, payments, refunds, shipments, tickets


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="ShopSim", lifespan=lifespan)
app.include_router(health.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(refunds.router)
app.include_router(shipments.router)
app.include_router(customers.router)
app.include_router(tickets.router)
