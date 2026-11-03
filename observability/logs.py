"""Logs JSON : une ligne par événement, sur stderr et dans `<log_dir>/<service>-<jour>.jsonl`.

Rotation (J7.1) : un fichier par jour UTC (`shopsim-2026-11-04.jsonl`), choisi par l'heure de
l'événement, jamais renommé ni tronqué. Plusieurs processus écrivent dans le même fichier (un
serveur MCP par exécution dans `mcp-…`) : chacun l'ouvre en ajout (`O_APPEND`) et écrit chaque
ligne en un seul appel système. Au changement de jour, un processus supprime les fichiers de
son nom plus vieux que `LOG_RETENTION_DAYS` (7 par défaut) ; une suppression déjà faite par un
autre est ignorée. L'ancien fichier sans date (`shopsim.jsonl`, d'avant J7.1) est relu par les
outils de lecture, et supprimé quand il n'a pas été écrit depuis la durée de conservation.

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
from datetime import UTC, date, datetime, timedelta
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
        # Enquête automatique de l'agent Ops (J6.5) : interrupteur, tri d'une alerte
        "ops_auto",
        "triage",
        "alert_id",
        # Alertes reçues d'Alertmanager (nom de la règle, gravité)
        "alertname",
        "severity",
        # Serveur MCP Ops : arguments des outils (listes fermées et nombres seulement)
        "log_service",
        "log_level",
        "status_filter",
        "since_minutes",
        "metric",
        "window",
        # Évaluation de l'agent Ops : étape en cours (nom de scénario ou de panne), seulement dans
        # `ops_eval.jsonl`, que l'agent ne lit jamais
        "phase",
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


RETENTION_ENV = "LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 7
SUFFIX = ".jsonl"
_DATED = re.compile(r"^(?P<stem>.+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl$")


def retention_days() -> int:
    """Durée de conservation, relue à chaque changement de jour ; une valeur invalide (pas un
    entier, ou moins d'un jour) donne la valeur par défaut : un log ne fait jamais tomber
    l'appelant, et ne supprime jamais le fichier du jour."""
    try:
        days = int(os.environ.get(RETENTION_ENV) or DEFAULT_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return days if days >= 1 else DEFAULT_RETENTION_DAYS


def dated_name(stem: str, day: date) -> str:
    return f"{stem}-{day.isoformat()}{SUFFIX}"


def segments(log_dir: Path, stem: str) -> list[tuple[date | None, Path]]:
    """Fichiers d'un nom de logs, du plus ancien au plus récent : l'ancien fichier sans date
    (`<stem>.jsonl`, date inconnue) d'abord, puis un fichier par jour. Nom exact : `mcp` ne
    prend jamais `ops_mcp-…`."""
    found: list[tuple[date | None, Path]] = []
    legacy = log_dir / f"{stem}{SUFFIX}"
    if legacy.exists() or legacy.is_symlink():
        found.append((None, legacy))
    dated = []
    for path in log_dir.glob(f"{stem}-*{SUFFIX}"):
        match = _DATED.match(path.name)
        if match is None or match["stem"] != stem:
            continue
        try:
            dated.append((date.fromisoformat(match["day"]), path))
        except ValueError:
            continue
    return found + sorted(dated)


def purge(log_dir: Path, stem: str, today: date, days: int) -> None:
    """Supprime les fichiers de ce nom plus vieux que `days` jours (l'ancien fichier sans date :
    selon sa dernière écriture). Déjà supprimé par un autre processus : ignoré."""
    oldest = today - timedelta(days=days)
    for day, path in segments(log_dir, stem):
        if day is None:
            written = datetime.fromtimestamp(path.lstat().st_mtime, UTC).date()
            expired = written < oldest
        else:
            expired = day < oldest
        if expired:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


class DailyFileHandler(logging.Handler):
    """Un fichier par jour UTC de l'événement, ouvert en ajout ; une ligne = un `write`."""

    def __init__(self, log_dir: Path, stem: str) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.stem = stem
        self._day: date | None = None
        self._fd: int | None = None

    def _open(self, day: date) -> int:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        path = self.log_dir / dated_name(self.stem, day)
        # Jamais à travers un lien symbolique
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        self._day = day
        try:
            purge(self.log_dir, self.stem, day, retention_days())
        except OSError:
            pass
        return self._fd

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8")
            day = datetime.fromtimestamp(record.created, UTC).date()
            fd = self._fd if day == self._day and self._fd is not None else self._open(day)
            # Un seul appel en général ; une écriture partielle (rare) est complétée
            while data:
                written = os.write(fd, data)
                data = data[written:]
        except Exception:  # noqa: BLE001  (comme logging : un log ne fait jamais tomber l'appelant)
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
                self._day = None
        finally:
            self.release()
        super().close()


def _handlers(
    service: str, log_dir: Path | None, filename: str, stream: TextIO | None
) -> list[logging.Handler]:
    formatter = JsonFormatter(service)
    handlers: list[logging.Handler] = []
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Fichier du jour, en ajout : plusieurs processus peuvent écrire dans le même
        handlers.append(DailyFileHandler(log_dir, filename.removesuffix(SUFFIX)))
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
    """Logs JSON du service : stderr (`stream`) et `<log_dir>/<service>-<jour>.jsonl`.

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
    """Logger écrit dans un seul nom de fichier (un fichier par jour), et nulle part ailleurs
    (ni stderr, ni la racine). Sans `log_dir`, rien n'est écrit."""
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


def read_lines(log_dir: Path, filename: str) -> list[str]:
    """Toutes les lignes écrites sous un nom de fichier (`shopsim.jsonl`), tous jours
    confondus, du plus ancien fichier au plus récent (tests, outils)."""
    lines: list[str] = []
    for _, path in segments(log_dir, filename.removesuffix(SUFFIX)):
        lines += path.read_text(encoding="utf-8").splitlines()
    return lines
