"""Appels HTTP du serveur Ops : adresses et chemins fixes, jamais pris des arguments d'un outil.

Sources : Prometheus, AgentPilot, ShopSim, Alertmanager. Aucun jeton ni en-tête Chaos : ce
serveur ne peut ni lire ni changer l'état des pannes. Un échec (injoignable, délai, réponse
illisible) devient `SourceUnavailable`, au message lisible par l'agent.
"""

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from observability import trace
from observability.logs import log_event

logger = logging.getLogger(__name__)

TIMEOUT_ENV = "OPS_HTTP_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 5.0


class Source(StrEnum):
    PROMETHEUS = "prometheus"
    AGENTPILOT = "agentpilot"
    SHOPSIM = "shopsim"
    ALERTMANAGER = "alertmanager"


@dataclass(frozen=True)
class Origin:
    label: str
    env: str
    default: str


ORIGINS = {
    Source.PROMETHEUS: Origin("Prometheus", "PROMETHEUS_URL", "http://localhost:9090"),
    Source.AGENTPILOT: Origin("AgentPilot", "AGENTPILOT_URL", "http://localhost:8001"),
    Source.SHOPSIM: Origin("ShopSim", "SHOPSIM_URL", "http://localhost:8000"),
    Source.ALERTMANAGER: Origin("Alertmanager", "ALERTMANAGER_URL", "http://localhost:9093"),
}


@dataclass(frozen=True)
class Endpoint:
    source: Source
    path: str


# Seuls chemins appelés par le serveur Ops (jamais `/chaos`)
PROMETHEUS_QUERY = Endpoint(Source.PROMETHEUS, "/api/v1/query")
PROMETHEUS_READY = Endpoint(Source.PROMETHEUS, "/-/ready")
ALERTMANAGER_READY = Endpoint(Source.ALERTMANAGER, "/-/ready")
AGENTPILOT_ALERTS = Endpoint(Source.AGENTPILOT, "/alerts")
AGENTPILOT_STATUS = Endpoint(Source.AGENTPILOT, "/control/status")
SHOPSIM_HEALTH = Endpoint(Source.SHOPSIM, "/health")
ENDPOINTS = (
    PROMETHEUS_QUERY,
    PROMETHEUS_READY,
    ALERTMANAGER_READY,
    AGENTPILOT_ALERTS,
    AGENTPILOT_STATUS,
    SHOPSIM_HEALTH,
)


class SourceUnavailable(Exception):
    """Source injoignable, trop lente ou en erreur : message destiné à l'agent."""


def base_url(source: Source) -> str:
    origin = ORIGINS[source]
    return os.environ.get(origin.env, origin.default).rstrip("/")


def timeout_s() -> float:
    return float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))


# Transport remplaçable par les tests (faux Prometheus, enregistrement des requêtes)
transport: httpx.AsyncBaseTransport | None = None


@dataclass
class Reply:
    status: int
    duration_ms: int
    body: Any  # JSON décodé, ou texte si la réponse n'est pas du JSON


async def fetch(endpoint: Endpoint, params: dict[str, str | int] | None = None) -> Reply:
    """GET sur un chemin fixe ; lève `SourceUnavailable` si la source ne répond pas."""
    source, path = endpoint.source, endpoint.path
    label = ORIGINS[source].label
    url = base_url(source)
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            base_url=url, timeout=timeout_s(), transport=transport
        ) as client:
            response = await client.get(path, params=params, headers=trace.headers())
    except httpx.TimeoutException as exc:
        _log(source, path, "timeout", None, start)
        raise SourceUnavailable(f"{label} n'a pas répondu en {timeout_s():g} s ({url}).") from exc
    except httpx.TransportError as exc:
        _log(source, path, "unreachable", None, start)
        raise SourceUnavailable(f"{label} est injoignable ({url}).") from exc
    _log(source, path, "ok" if response.is_success else "error", response.status_code, start)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:200]
    return Reply(response.status_code, round((time.perf_counter() - start) * 1000), body)


async def fetch_json(endpoint: Endpoint, params: dict[str, str | int] | None = None) -> Any:  # noqa: ANN401
    """GET dont la réponse doit être un succès en JSON ; sinon `SourceUnavailable`."""
    reply = await fetch(endpoint, params)
    label = ORIGINS[endpoint.source].label
    if not 200 <= reply.status < 300:
        raise SourceUnavailable(f"{label} a répondu {reply.status} sur {endpoint.path}.")
    if isinstance(reply.body, str):
        raise SourceUnavailable(f"Réponse illisible de {label} sur {endpoint.path}.")
    return reply.body


def _log(source: Source, path: str, outcome: str, status: int | None, start: float) -> None:
    log_event(
        logger,
        "source.call",
        logging.INFO if outcome == "ok" else logging.WARNING,
        tool=source.value,
        path=path,
        status=status,
        outcome=outcome,
        duration_ms=round((time.perf_counter() - start) * 1000),
    )
