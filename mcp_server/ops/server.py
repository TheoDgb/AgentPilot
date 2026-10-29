"""Serveur MCP de l'agent Ops (stdio), en lecture seule : logs, métriques, alertes, santé.

Lancement à la main : `.venv/bin/python -m mcp_server.ops.server`. Stdout est réservé au
protocole MCP.

Séparé du serveur de l'agent Support (`mcp_server.server`), dont il n'importe aucun outil :
aucun outil d'écriture, aucun outil sur les tickets, les commandes ou les clients. Aucun accès
au Chaos : ni `chaos.jsonl` (services de logs sur liste fixe, jamais un chemin), ni `/chaos`
(adresses et chemins fixes, `mcp_server/ops/sources.py`), ni jeton.

Variables : `LOG_DIR` (dossier des logs à lire, `logs/` par défaut ; aussi celui où le serveur
écrit ses propres logs, `ops_mcp.jsonl`, que les outils ne lisent jamais), `PROMETHEUS_URL`,
`AGENTPILOT_URL`, `SHOPSIM_URL`, `ALERTMANAGER_URL`, `OPS_HTTP_TIMEOUT_S` (5 s).

S'arrête avec le processus qui l'a lancé (`mcp_server/parent_watch.py`). Trace reçue dans
`TRACEPARENT`, sinon créée.
"""

import logging
import os

# Parent lu avant les imports lents : s'il meurt pendant ceux-ci, le serveur le voit quand même
PARENT_PID = os.getppid()

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

from mcp_server.ops.logs_tools import LOG_DIR_ENV, LOG_TOOLS  # noqa: E402
from mcp_server.ops.metrics_tools import METRIC_TOOLS  # noqa: E402
from mcp_server.ops.status_tools import STATUS_TOOLS  # noqa: E402
from mcp_server.parent_watch import exit_with_parent  # noqa: E402
from mcp_server.tool_logging import logged  # noqa: E402
from observability import trace  # noqa: E402
from observability.logs import configure, log_event  # noqa: E402

# Fichier de logs du serveur lui-même (`ops_mcp.jsonl`), hors de la liste lue par les outils
LOG_SERVICE = "ops_mcp"
# Seuls arguments d'outil écrits dans les logs (valeurs de listes fermées ou nombres), et le nom
# de leur champ : `service`, `level` et `status` ont déjà un sens dans chaque ligne
LOGGED_ARGUMENTS = {
    "service": "log_service",
    "since_minutes": "since_minutes",
    "level": "log_level",
    "status": "status_filter",
    "metric": "metric",
    "window": "window",
    "limit": "limit",
}

# Nom fixe : lancé par `python -m`, le module s'appelle `__main__`
logger = logging.getLogger("mcp_server.ops.server")

INSTRUCTIONS = (
    "Read-only observability of the ShopSim shop and of the AgentPilot platform: JSON logs, "
    "Prometheus metrics, alerts received by AgentPilot, service health. No tool changes "
    'anything. Every response gives the current real time ("now") and ages already '
    'computed ("ago"): logs, metrics and alerts use real wall-clock time, not the shop\'s '
    "simulated date. Prefer summaries (summarize_logs, query_metrics) before searching raw "
    "lines. Tool errors are readable messages: report them instead of guessing."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
TOOLS = [*LOG_TOOLS, *METRIC_TOOLS, *STATUS_TOOLS]


def build_server() -> MCPServer:
    server = MCPServer("ops", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.tool(annotations=READ_ONLY)(logged(tool, logger, LOGGED_ARGUMENTS))
    return server


mcp = build_server()


def main() -> None:
    exit_with_parent(PARENT_PID)
    # Le SDK MCP a posé à l'import un gestionnaire texte sur le logger racine : JSON seulement
    logging.getLogger().handlers.clear()
    configure(LOG_SERVICE, os.environ.get(LOG_DIR_ENV) or None)
    trace_id = trace.parse_traceparent(os.environ.get(trace.TRACEPARENT_ENV))
    with trace.bind(trace_id=trace_id or trace.new_trace_id()):
        log_event(logger, "mcp.started", mode="ops_read_only")
        mcp.run()


if __name__ == "__main__":
    main()
