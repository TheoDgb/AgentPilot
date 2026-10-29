"""Lecture des logs JSON, base des outils de logs du serveur MCP Ops (`mcp_server/ops/`).

Le service se choisit dans une liste fixe (`SERVICES`), jamais par un chemin : `chaos.jsonl`,
et tout autre fichier, restent hors de portée ; un lien symbolique est refusé.

Avec une fenêtre (`since`), le fichier est lu depuis la fin et la lecture s'arrête une fois la
fenêtre passée (à `ORDER_MARGIN` près) : les logs ne sont jamais découpés et grossissent vite.

En ligne de commande, les trois services fusionnés par heure :
`python -m observability.query --trace <trace_id>` (voir `--help`).
"""

import argparse
import json
import os
import sys
from collections import deque
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

SERVICES = ("shopsim", "mcp", "agentpilot")
LEVELS = ("debug", "info", "warning", "error", "critical")
MAX_LIMIT = 200
# Plusieurs processus écrivent dans un même fichier (un serveur MCP par exécution) : l'ordre des
# heures n'y est pas strict. La lecture depuis la fin continue jusqu'à cette marge avant `since`
ORDER_MARGIN = timedelta(seconds=60)
BLOCK_SIZE = 64 * 1024


def log_file(log_dir: Path, service: str) -> Path:
    if service not in SERVICES:
        raise ValueError(f"service inconnu : {service!r} (attendu : {', '.join(SERVICES)})")
    path = log_dir / f"{service}.jsonl"
    if path.is_symlink():
        raise ValueError(f"fichier de logs refusé (lien symbolique) : {service}")
    return path


def _parse(raw: bytes | str) -> dict[str, Any] | None:
    try:
        line = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Ligne en cours d'écriture par un autre processus
        return None
    return line if isinstance(line, dict) and isinstance(line.get("ts"), str) else None


def _open(path: Path, mode: str) -> IO[Any]:
    # Sans suivre de lien : un lien posé entre la vérification et l'ouverture est refusé aussi
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), mode, encoding=None)


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with _open(path, "rb") as file:
        for raw in file:
            line = _parse(raw)
            if line is not None:
                yield line


def _reversed_raw(path: Path) -> Iterator[bytes]:
    """Lignes brutes du fichier, de la dernière à la première, lues par blocs."""
    with _open(path, "rb") as file:
        position = file.seek(0, os.SEEK_END)
        rest = b""
        while position > 0:
            size = min(BLOCK_SIZE, position)
            position -= size
            file.seek(position)
            parts = (file.read(size) + rest).split(b"\n")
            rest = parts[0]
            for raw in reversed(parts[1:]):
                if raw.strip():
                    yield raw
        if rest.strip():
            yield rest


def recent_lines(log_dir: Path, service: str, since: datetime) -> Iterator[dict[str, Any]]:
    """Lignes depuis `since`, de la plus récente à la plus ancienne ; la lecture s'arrête une
    fois passée `since - ORDER_MARGIN`."""
    path = log_file(log_dir, service)
    if not path.is_file():
        return
    stop = since - ORDER_MARGIN
    for raw in _reversed_raw(path):
        line = _parse(raw)
        if line is None:
            continue
        at = datetime.fromisoformat(line["ts"])
        if at < stop:
            return
        if at >= since:
            yield line


def read_logs(
    log_dir: Path,
    service: str,
    *,
    since: datetime | None = None,
    level: str | None = None,
    trace_id: str | None = None,
    route: str | None = None,
    status_min: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Dernières lignes correspondant aux filtres (au plus `MAX_LIMIT`), dans l'ordre du fichier.

    `level` : niveau minimal ; `status_min` : code HTTP minimal (500 = erreurs serveur). Avec
    `since`, lecture depuis la fin du fichier (`recent_lines`).
    """
    path = log_file(log_dir, service)
    if level is not None and level not in LEVELS:
        raise ValueError(f"niveau inconnu : {level!r}")
    minimum = LEVELS.index(level) if level is not None else 0
    limit = max(1, min(limit, MAX_LIMIT))

    def keep(line: dict[str, Any]) -> bool:
        if line.get("level") in LEVELS and LEVELS.index(line["level"]) < minimum:
            return False
        if trace_id is not None and line.get("trace_id") != trace_id:
            return False
        if route is not None and line.get("route") != route:
            return False
        status = line.get("status")
        return status_min is None or (isinstance(status, int) and status >= status_min)

    if since is not None:
        newest: list[dict[str, Any]] = []
        for line in recent_lines(log_dir, service, since):
            if keep(line):
                newest.append(line)
                if len(newest) == limit:
                    break
        return newest[::-1]
    found: deque[dict[str, Any]] = deque(maxlen=limit)
    for line in _lines(path):
        if keep(line):
            found.append(line)
    return list(found)


# --- Ligne de commande -------------------------------------------------------------------

DEFAULT_DIR = Path("logs")
# Champs déjà affichés en tête de ligne
HEADER_FIELDS = ("ts", "service", "level", "event", "trace_id", "logger")


def merged(
    log_dir: Path,
    services: Sequence[str],
    limit: int = 100,
    **filters: Any,  # noqa: ANN401
) -> list[dict[str, Any]]:
    """Dernières lignes de plusieurs services, triées par heure."""
    lines = [line for s in services for line in read_logs(log_dir, s, limit=limit, **filters)]
    return sorted(lines, key=lambda line: line["ts"])[-limit:]


def _value(value: Any) -> str:  # noqa: ANN401
    # Guillemets seulement si nécessaire : `path=/orders/12`, `error="server closed…"`
    if isinstance(value, str) and (" " in value or not value):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def format_line(line: dict[str, Any]) -> str:
    """Une ligne lisible : heure, service, niveau, événement, puis les autres champs."""
    rest = " ".join(
        f"{key}={_value(value)}"
        for key, value in line.items()
        if key not in HEADER_FIELDS and key != "traceback" and value is not None
    )
    head = (
        f"{line['ts']} {line.get('service', '?'):<10} {line.get('level', '?'):<7} {line['event']}"
    )
    return f"{head} {rest}".rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observability.query",
        description="Lit les logs JSON de ShopSim, du serveur MCP et d'AgentPilot.",
    )
    parser.add_argument("--trace", help="trace_id à suivre (une exécution, une approbation)")
    parser.add_argument(
        "--service", action="append", choices=SERVICES, help="service (répétable ; tous sinon)"
    )
    parser.add_argument("--level", choices=LEVELS, help="niveau minimal")
    parser.add_argument("--route", help="gabarit de route, ex. /orders/{order_id}")
    parser.add_argument("--status-min", type=int, help="code HTTP minimal, ex. 500")
    parser.add_argument("--since", type=float, metavar="MINUTES", help="dernières minutes")
    parser.add_argument("--limit", type=int, default=100, help=f"lignes (max {MAX_LIMIT})")
    parser.add_argument("--json", action="store_true", help="lignes JSON brutes")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="dossier des logs")
    args = parser.parse_args(argv)

    since = datetime.now(UTC) - timedelta(minutes=args.since) if args.since else None
    lines = merged(
        args.dir,
        args.service or SERVICES,
        limit=args.limit,
        since=since,
        level=args.level,
        trace_id=args.trace,
        route=args.route,
        status_min=args.status_min,
    )
    for line in lines:
        print(json.dumps(line, ensure_ascii=False) if args.json else format_line(line))
    if not lines:
        print(f"Aucune ligne dans {args.dir}/ pour ces filtres.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
