"""Application FastAPI ShopSim.

Logs JSON (`observability`) : une ligne par requête, sur stderr et dans
`$LOG_DIR/shopsim.jsonl`. Les requêtes vers `/chaos` vont seulement dans
`$LOG_DIR/chaos.jsonl` (nulle part sans `LOG_DIR`) : l'activation d'une panne ne doit pas se
lire dans les logs que l'agent Ops consultera.

Métriques Prometheus (`GET /metrics`, J4.2) : toutes les requêtes sauf `/chaos` (même raison) et
`/metrics` lui-même, collecté toutes les 10 s, qui n'est pas non plus journalisé.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from observability.http import RequestLogMiddleware
from observability.logs import configure, configure_isolated
from shopsim.app.db import init_db
from shopsim.app.routers import (
    chaos,
    clock,
    customers,
    health,
    metrics,
    orders,
    payments,
    refunds,
    resends,
    shipments,
    tickets,
)

LOG_DIR_ENV = "LOG_DIR"
CHAOS_LOGGER = "shopsim.chaos"
METRICS_PATH = "/metrics"
logger = logging.getLogger("shopsim")
chaos_logger = logging.getLogger(CHAOS_LOGGER)
# Dès l'import : jamais sur stderr, même avant la configuration du lifespan (tests)
configure_isolated(CHAOS_LOGGER, "shopsim", None, "chaos.jsonl")


def is_chaos(route: str | None, path: str) -> bool:
    return any(p == "/chaos" or p.startswith("/chaos/") for p in (route or "", path))


def is_metered(route: str | None, path: str) -> bool:
    return not is_chaos(route, path) and path != METRICS_PATH


def request_logger(route: str | None, path: str) -> logging.Logger:
    return chaos_logger if is_chaos(route, path) else logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log_dir = os.environ.get(LOG_DIR_ENV) or None
    configure("shopsim", log_dir)
    configure_isolated(CHAOS_LOGGER, "shopsim", log_dir, "chaos.jsonl")
    init_db()
    yield


app = FastAPI(title="ShopSim", lifespan=lifespan)
# Healthcheck Docker (toutes les 5 s) et collecte de Prometheus (toutes les 10 s) : journalisés
# seulement en cas d'échec
app.add_middleware(
    RequestLogMiddleware,
    logger_for=request_logger,
    quiet_paths=frozenset({"/health", METRICS_PATH}),
    metered=is_metered,
)
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(clock.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(refunds.router)
app.include_router(resends.router)
app.include_router(shipments.router)
app.include_router(customers.router)
app.include_router(tickets.router)
app.include_router(chaos.router)
