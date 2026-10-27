"""Lecture des logs JSON, base du futur outil `search_logs` de l'agent Ops (J6).

Le service se choisit dans une liste fixe (`SERVICES`), jamais par un chemin : `chaos.jsonl`,
et tout autre fichier, restent hors de portée.

En ligne de commande, les trois services fusionnés par heure :
`python -m observability.query --trace <trace_id>` (voir `--help`).
"""

import argparse
import json
import sys
from collections import deque
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICES = ("shopsim", "mcp", "agentpilot")
LEVELS = ("debug", "info", "warning", "error", "critical")
MAX_LIMIT = 200


def log_file(log_dir: Path, service: str) -> Path:
    if service not in SERVICES:
        raise ValueError(f"service inconnu : {service!r} (attendu : {', '.join(SERVICES)})")
    return log_dir / f"{service}.jsonl"


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as file:
        for raw in file:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                # Ligne en cours d'écriture par un autre processus
                continue
            if isinstance(line, dict):
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

    `level` : niveau minimal ; `status_min` : code HTTP minimal (500 = erreurs serveur).
    """
    path = log_file(log_dir, service)
    if level is not None and level not in LEVELS:
        raise ValueError(f"niveau inconnu : {level!r}")
    minimum = LEVELS.index(level) if level is not None else 0
    limit = max(1, min(limit, MAX_LIMIT))
    found: deque[dict[str, Any]] = deque(maxlen=limit)
    for line in _lines(path):
        if since is not None and datetime.fromisoformat(line["ts"]) < since:
            continue
        if line.get("level") in LEVELS and LEVELS.index(line["level"]) < minimum:
            continue
        if trace_id is not None and line.get("trace_id") != trace_id:
            continue
        if route is not None and line.get("route") != route:
            continue
        status = line.get("status")
        if status_min is not None and not (isinstance(status, int) and status >= status_min):
            continue
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
