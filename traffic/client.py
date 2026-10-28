"""Client HTTP du générateur : liste blanche de routes, trace par visite, comptes par route.

Toute requête hors de `ALLOWED` lève `ForbiddenRequest` avant l'envoi : Chaos et les actions
(remboursement, renvoi, réponse, escalade, résolution) sont inaccessibles, même par erreur.
"""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from observability import trace

DEFAULT_URL = "http://localhost:8000"
TIMEOUT_S = 15.0
USER_AGENT = "shopsim-traffic"
# Code compté pour une requête sans réponse (connexion refusée, délai dépassé)
NO_RESPONSE = 0

# (méthode, gabarit de route) : seules requêtes possibles
ALLOWED: tuple[tuple[str, str], ...] = (
    ("GET", "/clock"),
    ("GET", "/customers/{customer_id}"),
    ("GET", "/customers/{customer_id}/orders"),
    ("GET", "/orders/{order_id}"),
    ("GET", "/payments/{order_id}"),
    ("GET", "/shipments/{order_id}"),
    ("GET", "/tickets"),
    ("POST", "/tickets"),
)


def _pattern(template: str) -> re.Pattern[str]:
    return re.compile(re.sub(r"\{\w+\}", r"[0-9]+", template))


_ROUTES = [(method, template, _pattern(template)) for method, template in ALLOWED]

# (méthode, chemin avec chaîne de requête, corps JSON, en-têtes) → (code, corps JSON ou None)
Send = Callable[[str, str, Any, Mapping[str, str]], tuple[int, Any]]


class ForbiddenRequest(Exception):
    """Requête hors de la liste blanche : jamais envoyée."""


def route_of(method: str, path: str) -> str:
    """Gabarit de la route autorisée ; lève `ForbiddenRequest` sinon."""
    bare = urllib.parse.urlsplit(path).path
    for allowed_method, template, pattern in _ROUTES:
        if method == allowed_method and pattern.fullmatch(bare):
            return template
    raise ForbiddenRequest(f"{method} {bare} n'est pas dans la liste blanche du générateur")


@dataclass
class Stats:
    """Comptes du générateur, partagés entre threads."""

    requests: Counter[str] = field(default_factory=Counter)
    dropped: int = 0
    tickets_created: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def request(self, method: str, route: str, status: int) -> None:
        with self.lock:
            self.requests[f"{method} {route} {status or 'error'}"] += 1

    def drop(self, count: int = 1) -> None:
        with self.lock:
            self.dropped += count

    def ticket(self) -> None:
        with self.lock:
            self.tickets_created += 1

    def take(self) -> tuple[dict[str, int], int, int]:
        """Comptes depuis le dernier appel, puis remise à zéro (compte rendu périodique)."""
        with self.lock:
            taken = (dict(self.requests), self.dropped, self.tickets_created)
            self.requests.clear()
            self.dropped = self.tickets_created = 0
            return taken


class ShopSimClient:
    def __init__(self, send: Send, stats: Stats | None = None) -> None:
        self._send = send
        self.stats = stats or Stats()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,  # noqa: ANN401
        headers: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> tuple[int, Any]:
        route = route_of(method, path)
        sent = {"User-Agent": USER_AGENT, **(headers or {})}
        if trace_id is not None:
            sent["traceparent"] = trace.traceparent(trace_id)
        status, body = self._send(method, path, payload, sent)
        self.stats.request(method, route, status)
        return status, body

    def get(self, path: str, trace_id: str | None = None) -> tuple[int, Any]:
        return self.request("GET", path, trace_id=trace_id)


def http_sender(base_url: str = DEFAULT_URL, timeout_s: float = TIMEOUT_S) -> Send:
    def send(method: str, path: str, payload: Any, headers: Mapping[str, str]) -> tuple[int, Any]:  # noqa: ANN401
        data = None
        all_headers = dict(headers)
        if payload is not None:
            data = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=data, method=method, headers=all_headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return NO_RESPONSE, None
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, None

    return send
