"""Métriques Prometheus de ShopSim : requêtes par route, méthode et code, et leur durée.

Les requêtes `/chaos` n'y figurent pas, et aucune métrique ne dit si une panne est active :
seuls ses symptômes (erreurs, latence) se voient, comme sur un vrai système.
"""

from fastapi import APIRouter, Response

from observability.metrics import CONTENT_TYPE_LATEST, HTTP_REGISTRY, exposition

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=Response)
def get_metrics() -> Response:
    return Response(exposition(HTTP_REGISTRY), media_type=CONTENT_TYPE_LATEST)
