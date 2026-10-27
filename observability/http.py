"""Middleware ASGI commun : trace de la requête et une ligne `http.request` par requête.

- Reprend le `trace_id` de l'en-tête `traceparent` s'il est valide, sinon en crée un, et le
  renvoie dans la réponse : un appelant sans trace peut retrouver la sienne.
- Une ligne par requête quand la réponse est envoyée (avant d'éventuelles tâches de fond) :
  méthode, gabarit de route, chemin, code, durée. Niveau warning pour 4xx, error pour 5xx.
- Exception non gérée : une ligne `http.exception` (type, message, traceback), puis une
  réponse 500 `{"detail": "Internal Server Error"}`, sans relancer l'exception (uvicorn
  l'écrirait une seconde fois, hors format).
- Jamais d'en-têtes, de corps ni de chaîne de requête : ni jeton, ni texte de client.
- Métriques (paramètre `metered`) : chaque requête mesurée alimente `observability.metrics`,
  avec le même code et la même durée que sa ligne de log (y compris le 500 d'une exception).
"""

import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from observability import metrics, trace
from observability.logs import error_type, log_event

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
# Logger d'une requête selon son gabarit de route et son chemin
LoggerFor = Callable[[str | None, str], logging.Logger]

INTERNAL_ERROR = json.dumps({"detail": "Internal Server Error"}).encode()


def route_template(scope: Scope) -> str | None:
    """Gabarit de la route (`/orders/{order_id}`), connu une fois la requête routée."""
    route = scope.get("route")
    return getattr(route, "path", None)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _level(status: int) -> int:
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO


class RequestLogMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        logger: logging.Logger | None = None,
        logger_for: LoggerFor | None = None,
        quiet_paths: frozenset[str] = frozenset(),
        metered: metrics.Metered | None = None,
    ) -> None:
        self.app = app
        default = logger or logging.getLogger("http")
        self.logger_for: LoggerFor = logger_for or (lambda route, path: default)
        # Chemins journalisés seulement en cas d'échec (>= 400), ex. un healthcheck fréquent
        self.quiet_paths = quiet_paths
        # Requêtes comptées dans les métriques HTTP (None : aucune)
        self.metered = metered

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id = trace.parse_traceparent(_header(scope, b"traceparent")) or trace.new_trace_id()
        with trace.bind(trace_id=trace_id):
            await self._handle(scope, receive, send, trace_id)

    async def _handle(self, scope: Scope, receive: Receive, send: Send, trace_id: str) -> None:
        start = time.perf_counter()
        state: dict[str, Any] = {"status": None, "logged": False}

        def log_request() -> None:
            if state["logged"]:
                return
            state["logged"] = True
            status = state["status"] or 500
            duration_s = time.perf_counter() - start
            route = route_template(scope)
            if self.metered is not None and self.metered(route, scope["path"]):
                metrics.observe(scope["method"], route, status, duration_s)
            if status < 400 and scope["path"] in self.quiet_paths:
                return
            log_event(
                self.logger_for(route, scope["path"]),
                "http.request",
                _level(status),
                method=scope["method"],
                route=route,
                path=scope["path"],
                status=status,
                duration_ms=round(duration_s * 1000),
            )

        async def send_logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"traceparent", trace.traceparent(trace_id).encode()))
                message["headers"] = headers
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                log_request()

        try:
            await self.app(scope, receive, send_logged)
        except Exception as exc:
            log_event(
                self.logger_for(route_template(scope), scope["path"]),
                "http.exception",
                logging.ERROR,
                error_type=error_type(exc),
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            if state["status"] is None:
                await send_logged(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send_logged({"type": "http.response.body", "body": INTERNAL_ERROR})
        finally:
            log_request()
