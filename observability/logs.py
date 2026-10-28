"""Logs JSON : une ligne par événement, sur stderr et dans `<log_dir>/<service>.jsonl`.

Chaque ligne a toujours `ts` (heure réelle UTC), `level`, `service`, `event`, `trace_id` (null
hors trace) et `logger`, puis le contexte lié (`run_id`, `ticket_id`, `action_id`) et les
champs de l'événement.

Les champs d'un événement passent par `log_event`, et seulement ceux de `FIELDS` : des
identifiants, des montants en centimes, des codes, des durées, jamais un texte libre venu d'un
client ou d'un agent (données personnelles). Un champ inconnu est ignoré en production et
refusé dans les tests (`STRICT`).

Filet anti-secret : les valeurs de `SECRET_ENV` présentes dans l'environnement, celles
déclarées par `register_secret` et tout motif de clé Anthropic sont remplacées dans la ligne
entière (message, erreur, traceback).
"""

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from observability import trace

FIELDS = frozenset(
    {
        # HTTP
        "method",
        "route",
        "path",
        "status",
        "duration_ms",
        # Erreurs
        "error_type",
        "error",
        "traceback",
        # Outils et actions
        "tool",
        "outcome",
        "mode",
        "decision",
        "reasons",
        "notice",
        # Identifiants et montants
        "run_id",
        "ticket_id",
        "action_id",
        "order_id",
        "customer_id",
        "amount_cents",
        "limit",
        "offset",
        # Exécutions et boucle
        "agent",
        "trigger",
        "cost_usd",
        "num_turns",
        "propose_only",
        "delay_s",
        "reason",
        "pause_reason",
        "emergency",
        "count",
        # Alertes reçues d'Alertmanager (nom de la règle, gravité)
        "alertname",
        "severity",
        # Générateur de trafic (compte rendu périodique) : requêtes par « méthode gabarit code »
        "requests",
        "dropped",
        "tickets_created",
        "rps",
    }
)
SECRET_ENV = ("ANTHROPIC_API_KEY", "CHAOS_TOKEN")
# Plus court, une valeur remplacée partout abîmerait les logs sans rien protéger
SECRET_MIN_LENGTH = 8
REDACTED = "[secret]"
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
# Bruit : une ligne par requête HTTP sortante ou par message MCP
QUIET_LOGGERS = ("httpx", "httpcore", "mcp", "claude_agent_sdk")

# Tests : un champ hors de FIELDS lève une erreur au lieu d'être ignoré
STRICT = False

_registered_secrets: set[str] = set()
_log_dir: Path | None = None


def register_secret(value: str | None) -> None:
    """Secret lu ailleurs que dans l'environnement (ex. clé d'API lue dans `.env`)."""
    if value and len(value) >= SECRET_MIN_LENGTH:
        _registered_secrets.add(value)


def scrub(text: str) -> str:
    values = {os.environ.get(name, "") for name in SECRET_ENV} | _registered_secrets
    for value in sorted(values, key=len, reverse=True):
        if len(value) >= SECRET_MIN_LENGTH:
            text = text.replace(value, REDACTED)
    return _ANTHROPIC_KEY.sub(REDACTED, text)


def error_type(exc: BaseException) -> str:
    """Nom qualifié d'une exception (`psycopg.OperationalError`, `KeyError`)."""
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__.split('.')[0]}.{cls.__qualname__}"


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    *,
    exc_info: bool = False,
    **fields: Any,  # noqa: ANN401
) -> None:
    """Écrit un événement ; seuls les champs de `FIELDS` sont gardés. `exc_info` : ajoute
    l'exception en cours (type, message, traceback)."""
    unknown = fields.keys() - FIELDS
    if unknown:
        if STRICT:
            raise ValueError(f"champs de log hors liste blanche : {sorted(unknown)}")
        fields = {key: value for key, value in fields.items() if key in FIELDS}
    logger.log(level, event, exc_info=exc_info, extra={"fields": fields})


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] | None = getattr(record, "fields", None)
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "event": record.msg if fields is not None else "log",
            "trace_id": trace.current_trace_id(),
            "logger": record.name,
        }
        line.update(trace.context())
        if fields is None:
            # Log ordinaire (uvicorn, bibliothèques) : son message, sans champs
            line["message"] = record.getMessage()
        else:
            line.update(fields)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            line.setdefault("error_type", error_type(exc))
            line.setdefault("error", str(exc))
            line["traceback"] = self.formatException(record.exc_info)
        return scrub(json.dumps(line, ensure_ascii=False, default=str))


def _handlers(
    service: str, log_dir: Path | None, filename: str, stream: TextIO | None
) -> list[logging.Handler]:
    formatter = JsonFormatter(service)
    handlers: list[logging.Handler] = []
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Ajout en fin de fichier : plusieurs processus peuvent écrire dans le même
        handlers.append(logging.FileHandler(log_dir / filename, encoding="utf-8", delay=True))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._observability = True  # type: ignore[attr-defined]
    return handlers


def _install(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    for handler in [h for h in logger.handlers if getattr(h, "_observability", False)]:
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers:
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def configure(
    service: str,
    log_dir: str | Path | None = None,
    *,
    logger_name: str | None = None,
    stream: TextIO | None = sys.stderr,
) -> None:
    """Logs JSON du service : stderr (`stream`) et `<log_dir>/<service>.jsonl`.

    Par défaut sur le logger racine, avec ceux de uvicorn (son journal d'accès est coupé : le
    middleware `observability.http` le remplace). `logger_name` : un seul logger, qui ne
    remonte plus à la racine (tests, plusieurs services dans un même processus).
    """
    global _log_dir
    # Absolu : transmis tel quel aux sous-processus, qui n'ont pas le même dossier courant
    directory = Path(log_dir).resolve() if log_dir else None
    target = logging.getLogger(logger_name)
    _install(target, _handlers(service, directory, f"{service}.jsonl", stream))
    if logger_name is not None:
        target.propagate = False
        return
    _log_dir = directory
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_isolated(
    logger_name: str, service: str, log_dir: str | Path | None, filename: str
) -> None:
    """Logger écrit dans un seul fichier, et nulle part ailleurs (ni stderr, ni la racine).
    Sans `log_dir`, rien n'est écrit."""
    logger = logging.getLogger(logger_name)
    handlers = _handlers(service, Path(log_dir) if log_dir else None, filename, None)
    _install(logger, handlers or [_null()])
    logger.propagate = False


def _null() -> logging.Handler:
    handler = logging.NullHandler()
    handler._observability = True  # type: ignore[attr-defined]
    return handler


def log_dir() -> Path | None:
    """Dossier des logs du service (configuré à la racine), à transmettre aux sous-processus."""
    return _log_dir
