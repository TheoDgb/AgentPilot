"""Métriques HTTP Prometheus communes (J4.2) et format d'exposition.

- `http_requests_total{method, route, status}` et `http_request_duration_seconds{method, route}`,
  alimentées par `RequestLogMiddleware` (paramètre `metered`).
- `route` est le gabarit (`/orders/{order_id}`), jamais le chemin : une requête sans route
  connue est rangée sous `unmatched`, et une méthode inhabituelle sous `other`. Aucune valeur
  d'étiquette ne vient donc librement de l'appelant (pas d'explosion du nombre de séries).

Registre propre (`HTTP_REGISTRY`) : seul un service qui mesure ses requêtes l'expose. Le
registre par défaut de `prometheus_client` ne garde que les métriques du processus et de
Python.
"""

from collections.abc import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

# Pas de séries `*_created` : elles doublent chaque compteur sans servir aux tableaux de bord
disable_created_metrics()

__all__ = ["CONTENT_TYPE_LATEST", "HTTP_REGISTRY", "Metered", "exposition", "observe"]

UNMATCHED = "unmatched"
OTHER_METHOD = "other"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# De 5 ms à 10 s : couvre la latence normale des Paiements (100 ms), la panne de latence (4 s)
# et le plafond de la saturation (5 s)
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 7.5, 10.0)

# Requête à mesurer, selon son gabarit de route et son chemin
Metered = Callable[[str | None, str], bool]

HTTP_REGISTRY = CollectorRegistry()
REQUESTS = Counter(
    "http_requests_total",
    "Requêtes HTTP traitées, par méthode, gabarit de route et code de réponse.",
    ["method", "route", "status"],
    registry=HTTP_REGISTRY,
)
DURATION = Histogram(
    "http_request_duration_seconds",
    "Durée des requêtes HTTP (jusqu'à l'envoi complet de la réponse), en secondes.",
    ["method", "route"],
    buckets=DURATION_BUCKETS,
    registry=HTTP_REGISTRY,
)


def observe(method: str, route: str | None, status: int, duration_s: float) -> None:
    method = method if method in METHODS else OTHER_METHOD
    route = route or UNMATCHED
    REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    DURATION.labels(method=method, route=route).observe(duration_s)


def exposition(*registries: CollectorRegistry) -> bytes:
    """Format texte de Prometheus : métriques du processus, puis celles des registres donnés."""
    return b"".join(generate_latest(registry) for registry in (REGISTRY, *registries))
