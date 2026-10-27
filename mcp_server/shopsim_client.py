"""Client HTTP vers l'API de ShopSim : le serveur MCP ne touche jamais la base.

Toute erreur (404, 409, 422, 5xx, délai dépassé, service injoignable) devient une
`ShopSimError` dont le message, lisible, est renvoyé tel quel à l'agent.
Aucun accès au Chaos : ce client n'envoie jamais `X-Chaos-Token`.

Chaque requête porte la trace courante (en-tête `traceparent`) et écrit une ligne
`shopsim.call` (méthode, chemin, code, durée), sans corps ni paramètres de requête.
"""

import logging
import os
import time
from typing import Any

import httpx

from observability import trace
from observability.logs import log_event

logger = logging.getLogger(__name__)

DEFAULT_SHOPSIM_URL = "http://localhost:8000"
# Plus que les 4 s de la panne payment_latency : la lenteur reste visible sans échec
DEFAULT_TIMEOUT_S = 10.0


class ShopSimError(Exception):
    """Échec d'un appel à ShopSim, avec un message destiné à l'agent."""


def shopsim_url() -> str:
    return os.environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL).rstrip("/")


def timeout_s() -> float:
    return float(os.environ.get("SHOPSIM_TIMEOUT_S", DEFAULT_TIMEOUT_S))


def _client() -> httpx.AsyncClient:
    # URL et délai relus à chaque appel
    return httpx.AsyncClient(base_url=shopsim_url(), timeout=timeout_s())


class ShopSimUnavailable(ShopSimError):
    """ShopSim lent, injoignable ou en erreur interne (5xx) : on ne sait pas si une écriture a
    eu lieu. Un réessai avec la même clé d'idempotence est sans danger."""


class ShopSimConflict(ShopSimError):
    """409 de ShopSim : action incompatible avec l'état actuel ou avec une clé déjà utilisée."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def _outcome(status: int) -> str:
    if status in (200, 201):
        return "ok"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    return "unavailable" if status >= 500 else "refused"


def _detail(response: httpx.Response) -> str:
    """Motif lisible d'un refus de ShopSim (champ `detail` de FastAPI)."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return response.text[:200]
    if isinstance(detail, dict):
        # Ex. remboursement trop élevé : {"message", "remaining_cents"}
        extra = ", ".join(f"{k} = {v}" for k, v in detail.items() if k != "message")
        return f"{detail.get('message', '')} ({extra})" if extra else str(detail.get("message"))
    if isinstance(detail, list):
        # Erreurs de validation : champ et message
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:])} : {e.get('msg')}" for e in detail
        )
    return str(detail)


async def _send(
    method: str,
    path: str,
    not_found: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """Appel à ShopSim ; toute réponse autre que 200 ou 201 devient une `ShopSimError`."""
    endpoint = f"{method} {path}"
    headers = trace.headers()
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    start = time.perf_counter()

    def log_call(outcome: str, status: int | None = None) -> None:
        log_event(
            logger,
            "shopsim.call",
            logging.INFO if outcome in ("ok", "not_found", "conflict") else logging.WARNING,
            method=method,
            path=path,
            status=status,
            outcome=outcome,
            duration_ms=round((time.perf_counter() - start) * 1000),
        )

    try:
        async with _client() as client:
            response = await client.request(method, path, params=params, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        log_call("timeout")
        raise ShopSimUnavailable(
            f"ShopSim n'a pas répondu en {timeout_s():g} s sur {endpoint} (service lent). "
            "Réessayer plus tard ou escalader."
        ) from exc
    except httpx.TransportError as exc:
        log_call("unreachable")
        raise ShopSimUnavailable(f"ShopSim est injoignable à {shopsim_url()}.") from exc

    status = response.status_code
    log_call(_outcome(status), status)
    if status in (200, 201):
        return response
    if status == 404:
        raise ShopSimError(not_found)
    if status >= 500:
        raise ShopSimUnavailable(
            f"ShopSim a renvoyé une erreur interne ({status}) sur {endpoint}. "
            "Le service est peut-être en panne : réessayer plus tard ou escalader."
        )
    detail = _detail(response)
    if status == 409:
        raise ShopSimConflict(f"ShopSim a refusé l'action (409) sur {endpoint} : {detail}", detail)
    raise ShopSimError(f"ShopSim a refusé la requête ({status}) sur {endpoint} : {detail}")


def _json(response: httpx.Response) -> Any:  # noqa: ANN401
    try:
        return response.json()
    except ValueError as exc:
        request = response.request
        raise ShopSimError(
            f"Réponse illisible de ShopSim sur {request.method} {request.url.path}."
        ) from exc


async def get_json(path: str, not_found: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    """GET sur ShopSim ; `not_found` est le message renvoyé si la ressource n'existe pas."""
    return _json(await _send("GET", path, not_found, params=params))


async def post_json(
    path: str,
    not_found: str,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, Any]:
    """POST sur ShopSim ; renvoie le code (201 = créé, 200 = rejeu ou déjà fait) et le JSON."""
    response = await _send("POST", path, not_found, body=body, idempotency_key=idempotency_key)
    return response.status_code, _json(response)
