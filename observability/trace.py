"""Trace d'une exécution, transmise d'un service à l'autre.

Le `trace_id` (32 caractères hexadécimaux, format W3C Trace Context) est créé par AgentPilot
pour chaque exécution. Il voyage :
- entre processus par l'environnement (`TRACEPARENT`, convention d'OpenTelemetry) : AgentPilot
  le donne au serveur MCP qu'il fait lancer ;
- en HTTP par l'en-tête standard `traceparent` : le client ShopSim l'ajoute à chaque requête.

Dans un processus, la trace et le contexte (exécution, ticket, action) sont dans des
`contextvars` : chaque tâche asyncio, et `asyncio.to_thread`, en hérite. Deux exécutions en
parallèle gardent chacune les leurs.
"""

import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

HEADER = "traceparent"
TRACEPARENT_ENV = "TRACEPARENT"
# Contexte qu'un service peut lier à ses logs ; tout le reste passe par les champs d'événement
CONTEXT_FIELDS = frozenset({"run_id", "ticket_id", "action_id"})

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_INVALID_TRACE = "0" * 32

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_context: ContextVar[Mapping[str, Any] | None] = ContextVar("log_context", default=None)


def new_trace_id() -> str:
    return secrets.token_hex(16)


def parse_traceparent(value: str | None) -> str | None:
    """`trace_id` d'un en-tête `traceparent` valide ; None s'il est absent ou invalide."""
    if not value:
        return None
    match = _TRACEPARENT.match(value.strip().lower())
    if match is None or match.group(1) == _INVALID_TRACE:
        return None
    return match.group(1)


def traceparent(trace_id: str) -> str:
    """En-tête `traceparent` pour cette trace (nouvel identifiant d'étape à chaque appel)."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def current_trace_id() -> str | None:
    return _trace_id.get()


def context() -> Mapping[str, Any]:
    return _context.get() or {}


def headers() -> dict[str, str]:
    """En-têtes à ajouter à une requête sortante : la trace courante, s'il y en a une."""
    trace_id = current_trace_id()
    return {HEADER: traceparent(trace_id)} if trace_id else {}


@contextmanager
def bind(trace_id: str | None = None, **fields: Any) -> Iterator[None]:  # noqa: ANN401
    """Lie une trace (si donnée) et du contexte aux logs émis dans ce bloc."""
    unknown = fields.keys() - CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"contexte de log inconnu : {sorted(unknown)}")
    trace_token = _trace_id.set(trace_id) if trace_id is not None else None
    context_token = _context.set({**context(), **fields})
    try:
        yield
    finally:
        _context.reset(context_token)
        if trace_token is not None:
            _trace_id.reset(trace_token)
