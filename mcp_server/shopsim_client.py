"""Client HTTP vers l'API de ShopSim : le serveur MCP ne touche jamais la base.

Toute erreur (404, 5xx, délai dépassé, service injoignable) devient une `ShopSimError`
dont le message, lisible, est renvoyé tel quel à l'agent.
Aucun accès au Chaos : ce client n'envoie jamais `X-Chaos-Token`.
"""

import os
from typing import Any

import httpx

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


async def get_json(path: str, not_found: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    """GET sur ShopSim ; `not_found` est le message renvoyé si la ressource n'existe pas."""
    endpoint = f"GET {path}"
    try:
        async with _client() as client:
            response = await client.get(path, params=params)
    except httpx.TimeoutException as exc:
        raise ShopSimError(
            f"ShopSim n'a pas répondu en {timeout_s():g} s sur {endpoint} (service lent). "
            "Réessayer plus tard ou escalader."
        ) from exc
    except httpx.TransportError as exc:
        raise ShopSimError(f"ShopSim est injoignable à {shopsim_url()}.") from exc

    if response.status_code == 404:
        raise ShopSimError(not_found)
    if response.status_code >= 500:
        raise ShopSimError(
            f"ShopSim a renvoyé une erreur interne ({response.status_code}) sur {endpoint}. "
            "Le service est peut-être en panne : réessayer plus tard ou escalader."
        )
    if response.status_code != 200:
        raise ShopSimError(
            f"ShopSim a refusé la requête ({response.status_code}) sur {endpoint} : "
            f"{response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ShopSimError(f"Réponse illisible de ShopSim sur {endpoint}.") from exc
