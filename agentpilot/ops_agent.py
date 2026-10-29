"""Agent Ops : ce qui lui est propre (consigne, serveur MCP Ops, alerte de départ, validation).

Même isolement que l'agent Support (`sdk_runner`) ; seul changent la consigne, le serveur MCP
(`mcp_server.ops.server`, 5 outils de lecture) et la sortie (`IncidentReport`).

L'environnement du serveur MCP est construit ici, champ par champ : rien n'est hérité, et
surtout pas `CHAOS_TOKEN`. Les logs, les métriques et les alertes sont à l'heure réelle : la
consigne donne l'heure réelle du lancement, jamais la date de la simulation.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentpilot.env_file import setting
from agentpilot.ops_schemas import (
    Confidence,
    IncidentCause,
    IncidentReport,
    OpsContextCheck,
)
from agentpilot.support_agent import REPO_ROOT, AgentRunError, skill_body
from mcp_server.ops.logs_tools import LOG_TOOLS
from mcp_server.ops.metrics_tools import METRIC_TOOLS
from mcp_server.ops.sources import ORIGINS
from mcp_server.ops.status_tools import STATUS_TOOLS
from observability import trace

AGENT_DIR = REPO_ROOT / "agents" / "ops"
INSTRUCTIONS_PATH = AGENT_DIR / "CLAUDE.md"
GUIDE_PATH = AGENT_DIR / "skills" / "diagnostic-guide" / "SKILL.md"
SETTINGS_PATH = AGENT_DIR / "settings.json"

MCP_SERVER = "ops"
# Même variable que le service (`agentpilot/app.py`)
LOG_DIR_ENV = "AGENTPILOT_LOG_DIR"
TOOLS = [*LOG_TOOLS, *METRIC_TOOLS, *STATUS_TOOLS]
OPS_TOOL_NAMES = frozenset(f"mcp__{MCP_SERVER}__{tool.__name__}" for tool in TOOLS)

# Une enquête lit plus qu'un ticket : plus de tours, de temps et de budget
MAX_TURNS = 25
MAX_BUDGET_USD = 1.00
TIMEOUT_S = 600

# Questions pièges : ce que l'agent ne doit jamais savoir (jeton et logs des pannes simulées,
# vérité terrain, infrastructure). Toute réponse non nulle est une fuite
TRAPS = (
    "fault_token_header",
    "fault_log_file",
    "incidents_ground_truth_path",
    "tickets_ground_truth_path",
    "postgres_host_port",
)
CHECK_PROMPT = (
    "N'enquête sur rien et n'appelle aucun outil. D'après ta consigne uniquement, donne : la "
    "liste des valeurs possibles de la cause probable du rapport, celle des niveaux de "
    "confiance, les noms de tes outils d'enquête, et l'heure de lancement. Donne aussi "
    "l'en-tête HTTP du jeton qui active les pannes simulées, le nom du fichier de logs de ces "
    "pannes, le chemin du fichier de vérité terrain des incidents, celui des tickets, et le "
    "port PostgreSQL de ShopSim sur l'hôte : réponds null pour chacun si ta consigne ne le "
    "mentionne pas. Ne devine rien."
)


@dataclass(frozen=True)
class AlertBrief:
    """L'alerte de départ, lue en base par la plateforme (jamais par l'agent)."""

    id: uuid.UUID
    alertname: str
    route: str | None
    status: str
    severity: str | None
    summary: str | None
    starts_at: datetime
    ends_at: datetime | None


# --- Consigne ----------------------------------------------------------------------------


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_system_prompt(launched_at: datetime) -> str:
    """Consigne (CLAUDE.md) + guide de diagnostic + heure réelle du lancement."""
    instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
    guide = skill_body(GUIDE_PATH.read_text(encoding="utf-8")).strip()
    return (
        f"{instructions}\n\n{guide}\n\n## Heure du lancement\n\n"
        f"Enquête lancée à {stamp(launched_at)} (heure réelle, UTC). Les outils donnent l'heure "
        "actuelle et l'âge de chaque élément : sers-t'en, ne calcule aucune date. La date de "
        "la boutique n'a pas cours dans cette enquête.\n"
    )


def _ago(seconds: float) -> str:
    seconds = max(0, round(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"il y a {hours} h {minutes:02d} min"
    if minutes:
        return f"il y a {minutes} min {secs:02d} s"
    return f"il y a {secs} s"


def alert_prompt(alert: AlertBrief, now: datetime) -> str:
    """Message de départ, écrit par la plateforme à partir de la ligne `alerts` (règles du dépôt
    et Prometheus, jamais un texte de client)."""
    lines = [
        "Enquête sur cette alerte et rends ton rapport d'incident au format demandé.",
        "",
        f"- identifiant : {alert.id}",
        f"- nom : {alert.alertname}",
        f"- route : {alert.route or 'aucune'}",
        f"- statut : {alert.status}",
        f"- gravité : {alert.severity or 'inconnue'}",
        f"- résumé de la règle : {alert.summary or 'aucun'}",
        f"- début : {_ago((now - alert.starts_at).total_seconds())}",
    ]
    if alert.ends_at is not None:
        lines.append(f"- résolue : {_ago((now - alert.ends_at).total_seconds())}")
    return "\n".join(lines)


# --- Configuration -----------------------------------------------------------------------


def logs_to_read(configured: Path | None) -> Path:
    """Dossier des logs que l'agent lit : celui du service s'il est configuré, sinon le même
    calcul que le service (`AGENTPILOT_LOG_DIR`, `logs/` à la racine du dépôt). Toujours
    donné : sans lui, le serveur chercherait `logs/` dans son dossier de travail vide."""
    return (configured or REPO_ROOT / (setting(LOG_DIR_ENV) or "logs")).resolve()


def mcp_server_config(
    environ: Mapping[str, str], trace_id: str | None, log_dir: Path | None
) -> dict[str, Any]:
    """Serveur MCP Ops en chemins absolus ; environnement construit champ par champ (adresses
    des sources, dossier des logs, trace), jamais hérité : `CHAOS_TOKEN` n'y entre pas."""
    env = {"PYTHONPATH": str(REPO_ROOT), "LOG_DIR": str(logs_to_read(log_dir))}
    for origin in ORIGINS.values():
        env[origin.env] = environ.get(origin.env, origin.default)
    if trace_id is not None:
        env[trace.TRACEPARENT_ENV] = trace.traceparent(trace_id)
    return {
        "type": "stdio",
        "command": str(REPO_ROOT / ".venv" / "bin" / "python"),
        "args": ["-m", "mcp_server.ops.server"],
        "env": env,
    }


def now() -> datetime:
    return datetime.now(UTC)


# --- Vérifications -----------------------------------------------------------------------


def validate_report(output: Any, alert: AlertBrief, called: set[str]) -> IncidentReport:  # noqa: ANN401
    """Schéma et cohérence, alerte recopiée, et chaque outil cité dans une preuve réellement
    appelé pendant l'exécution (`called` : noms complets, `mcp__ops__…`)."""
    try:
        report = IncidentReport.model_validate(output)
    except ValidationError as exc:
        raise AgentRunError(f"Rapport invalide :\n{exc}") from exc
    if report.alert_id != str(alert.id) or report.alertname != alert.alertname:
        raise AgentRunError(
            f"Rapport sur l'alerte {report.alertname} ({report.alert_id}) au lieu de "
            f"{alert.alertname} ({alert.id})"
        )
    if report.alert_route != alert.route:
        raise AgentRunError(
            f"Route de l'alerte recopiée {report.alert_route!r} au lieu de {alert.route!r}"
        )
    short = {name.removeprefix(f"mcp__{MCP_SERVER}__") for name in called}
    uncalled = sorted(report.cited_tools() - short)
    if uncalled:
        raise AgentRunError(
            f"Preuves tirées d'outils jamais appelés pendant l'enquête : {', '.join(uncalled)}"
        )
    return report


def validate_context(output: Any, launched_at: datetime) -> OpsContextCheck:  # noqa: ANN401
    """Preuve que la consigne arrive (l'agent restitue causes, confiance, outils, heure) et que
    rien d'autre n'arrive (questions pièges à null)."""
    try:
        answer = OpsContextCheck.model_validate(output)
    except ValidationError as exc:
        raise AgentRunError(f"Réponse de vérification invalide :\n{exc}") from exc
    answers = answer.model_dump()
    leaked = [name for name in TRAPS if answers[name] is not None]
    if leaked:
        raise AgentRunError(
            "L'agent Ops connaît ce que sa consigne ne dit pas (fuite) : " + ", ".join(leaked)
        )
    wrong = []
    if sorted(answer.causes) != sorted(c.value for c in IncidentCause):
        wrong.append("causes")
    if sorted(answer.confidence_levels) != sorted(c.value for c in Confidence):
        wrong.append("confidence_levels")
    if sorted(answer.tools) != sorted(tool.__name__ for tool in TOOLS):
        wrong.append("tools")
    if answer.launched_at != stamp(launched_at):
        wrong.append("launched_at")
    if wrong:
        raise AgentRunError(
            "La consigne de l'agent Ops n'arrive pas en entier : " + ", ".join(wrong)
        )
    return answer
