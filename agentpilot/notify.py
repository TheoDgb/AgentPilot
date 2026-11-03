"""Canal Discord (J7.2) : un message court à la fin de chaque enquête automatique de l'agent Ops.

Les alertes elles-mêmes vont sur le même canal par Alertmanager
(`monitoring/alertmanager/alertmanager-discord.yml`), pas par ce module : le canal ne dépend
pas d'AgentPilot pour elles.

- Adresse du webhook : `AGENTPILOT_DISCORD_WEBHOOK_URL`, un secret. Dans le conteneur, elle
  arrive par l'environnement et en est **retirée au démarrage** du service (`capture`) : la CLI
  de l'agent et les serveurs MCP héritent de cet environnement et ne la voient jamais. En
  local, elle est lue dans la seule ligne de `.env` (`env_file.setting`). Masquée dans les logs.
  Une adresse qui n'est pas celle d'un webhook Discord est refusée (un seul avertissement, sans
  la valeur) : une erreur de configuration n'envoie jamais rien ailleurs.
- Sans adresse : rien n'est envoyé, sans erreur.
- Contenu sur liste blanche, construit par du code : nom de l'alerte, route (gabarit), cause et
  confiance (énumérations), nombre d'alertes couvertes, lien vers la page de l'enquête. Jamais
  de texte libre de l'agent (résumé, correctif, preuves), qui pourrait recopier une valeur lue.
- Pas de rafale : au plus `AGENTPILOT_NOTIFY_MAX_PER_HOUR` messages par heure glissante (10 par
  défaut, compté dans ce processus) ; au-delà, le message est abandonné (log `notify.dropped`),
  l'enquête reste sur `/investigations`.
- Une seule tentative (5 s) : un échec est journalisé (`notify.failed`, code HTTP) et ne fait
  jamais échouer l'enquête. Mentions désactivées (`allowed_mentions`) : jamais `@everyone`.
"""

import logging
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from agentpilot import metrics
from agentpilot.env_file import setting
from agentpilot.ops_schemas import Confidence, IncidentCause
from observability.logs import log_event, register_secret

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "AGENTPILOT_DISCORD_WEBHOOK_URL"
PUBLIC_URL_ENV = "AGENTPILOT_PUBLIC_URL"
MAX_PER_HOUR_ENV = "AGENTPILOT_NOTIFY_MAX_PER_HOUR"
DEFAULT_PUBLIC_URL = "http://localhost:8001"
DEFAULT_MAX_PER_HOUR = 10
TIMEOUT_S = 5.0
WINDOW = timedelta(hours=1)
WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
# Discord refuse un message de plus de 2 000 caractères
CONTENT_MAX = 2_000

CAUSES = {
    IncidentCause.DATABASE_ERRORS: "erreurs de la base de données",
    IncidentCause.RESOURCE_SATURATION: "saturation des ressources",
    IncidentCause.SLOW_DEPENDENCY: "dépendance lente",
    IncidentCause.APPLICATION_ERROR: "erreur applicative",
    IncidentCause.SERVICE_UNREACHABLE: "service injoignable",
    IncidentCause.AGENT_PLATFORM: "plateforme des agents",
    IncidentCause.NO_INCIDENT: "aucun incident",
    IncidentCause.UNDETERMINED: "indéterminée",
}
CONFIDENCES = {Confidence.HIGH: "élevée", Confidence.MEDIUM: "moyenne", Confidence.LOW: "faible"}

# Envoi : (adresse, corps JSON) → code HTTP. Remplaçable par les tests (aucun envoi réel)
Post = Callable[[str, dict[str, Any]], Awaitable[int]]

_captured: str | None = None
_warned = False


def capture(environ: MutableMapping[str, str] = os.environ) -> None:
    """Au démarrage du service : l'adresse quitte l'environnement du processus (dont héritent
    la CLI et les serveurs MCP) et reste en mémoire, masquée dans les logs."""
    global _captured
    value = environ.pop(WEBHOOK_ENV, None)
    if value:
        _captured = value.strip()
        register_secret(_captured)


def webhook_url() -> str | None:
    """Adresse du webhook, ou None : absente, ou refusée parce qu'elle n'est pas celle d'un
    webhook Discord (un seul avertissement, sans la valeur)."""
    global _warned
    value = _captured or setting(WEBHOOK_ENV)
    if not value:
        return None
    register_secret(value)
    if not value.startswith(WEBHOOK_PREFIXES):
        if not _warned:
            _warned = True
            log_event(logger, "notify.disabled", logging.WARNING, reason="adresse non Discord")
        return None
    return value


def configured() -> bool:
    """Canal utilisable (page de pilotage : jamais l'adresse elle-même)."""
    return webhook_url() is not None


def public_url() -> str:
    return (setting(PUBLIC_URL_ENV) or DEFAULT_PUBLIC_URL).rstrip("/")


def max_per_hour() -> int:
    value = setting(MAX_PER_HOUR_ENV)
    try:
        return max(int(value), 0) if value else DEFAULT_MAX_PER_HOUR
    except ValueError:
        return DEFAULT_MAX_PER_HOUR


@dataclass(frozen=True)
class Investigation:
    """Ce qu'un message dit d'une enquête : identifiants et énumérations seulement."""

    run_id: uuid.UUID
    alertname: str
    route: str | None
    covered: int
    cause: IncidentCause | None = None
    confidence: Confidence | None = None
    # Enquête en échec par la faute de l'agent : sa catégorie (`sdk_runner.ErrorType`)
    error_type: str | None = None

    @classmethod
    def from_output(
        cls,
        run_id: uuid.UUID,
        alertname: str,
        route: str | None,
        covered: int,
        output: dict[str, Any] | None,
        error_type: str | None,
    ) -> "Investigation":
        """Seules la cause et la confiance sont lues dans le rapport, et seulement si ce sont
        des valeurs des énumérations."""
        cause = confidence = None
        if error_type is None and output is not None:
            try:
                cause = IncidentCause(output.get("probable_cause"))
                confidence = Confidence(output.get("confidence"))
            except ValueError:
                cause = confidence = None
        return cls(run_id, alertname, route, covered, cause, confidence, error_type)


def message(found: Investigation, base_url: str) -> str:
    where = f"{found.alertname} sur {found.route}" if found.route else found.alertname
    if found.covered:
        plural = "s" if found.covered > 1 else ""
        where += f" (+{found.covered} alerte{plural} couverte{plural})"
    link = f"{base_url}/investigations/{found.run_id}"
    if found.cause is None or found.confidence is None:
        detail = f" ({found.error_type})" if found.error_type else ""
        lines = [f"⚠️ Enquête en échec{detail} — {where}", "Rapport à lire par un humain.", link]
    else:
        lines = [
            f"🔎 Enquête terminée — {where}",
            f"Cause probable : {CAUSES[found.cause]} · confiance : {CONFIDENCES[found.confidence]}",
            link,
        ]
    return "\n".join(lines)[:CONTENT_MAX]


async def http_post(url: str, body: dict[str, Any]) -> int:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.post(url, json=body)
    return response.status_code


class Notifier:
    """Envoi des messages de fin d'enquête, avec la limite de fréquence (heure glissante)."""

    def __init__(self, post: Post = http_post) -> None:
        self.post = post
        self.sent: deque[datetime] = deque()

    def _allowed(self, now: datetime) -> bool:
        while self.sent and now - self.sent[0] >= WINDOW:
            self.sent.popleft()
        return len(self.sent) < max_per_hour()

    async def investigation_finished(self, found: Investigation, now: datetime) -> str | None:
        """Envoie le message ; renvoie `sent`, `failed`, `dropped`, ou None sans canal."""
        url = webhook_url()
        if url is None:
            return None
        if not self._allowed(now):
            return self._result("dropped", found, logging.WARNING)
        self.sent.append(now)
        body = {"content": message(found, public_url()), "allowed_mentions": {"parse": []}}
        try:
            status = await self.post(url, body)
        except (httpx.HTTPError, OSError) as exc:
            return self._result("failed", found, logging.ERROR, error_type=type(exc).__name__)
        if status >= 300:
            return self._result("failed", found, logging.ERROR, status=status)
        return self._result("sent", found, logging.INFO, status=status)

    @staticmethod
    def _result(result: str, found: Investigation, level: int, **fields: Any) -> str:  # noqa: ANN401
        metrics.notification(result)
        log_event(
            logger,
            f"notify.{result}",
            level,
            run_id=found.run_id,
            alertname=found.alertname,
            **fields,
        )
        return result
