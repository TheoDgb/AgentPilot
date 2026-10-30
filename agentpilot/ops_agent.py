"""Agent Ops : ce qui lui est propre (consigne, serveur MCP Ops, alerte de départ, validation).

Même isolement que l'agent Support (`sdk_runner`) ; seul changent la consigne, le serveur MCP
(`mcp_server.ops.server`, 5 outils de lecture) et la sortie (`IncidentReport`).

Deux architectures (J6.4, réglage `AGENTPILOT_OPS_ARCHITECTURE`) :
- `solo` (défaut) : un agent avec les 5 outils ;
- `subagents` : un enquêteur principal (`Agent`, `list_alerts`, `check_health`) qui délègue à
  deux subagents, `logs-investigator` (`search_logs`, `summarize_logs`) et
  `metrics-investigator` (`query_metrics`). Consignes dans `agents/ops-subagents/`, en
  complément de celle de l'agent seul, qui ne change pas. Chaque agent n'a que ses outils, et
  l'outil `Agent` ne lance que ces deux types (`hooks.OpsHooks`).

L'environnement du serveur MCP est construit ici, champ par champ : rien n'est hérité, et
surtout pas `CHAOS_TOKEN`. Les logs, les métriques et les alertes sont à l'heure réelle : la
consigne donne l'heure réelle du lancement, jamais la date de la simulation.
"""

import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition
from pydantic import ValidationError

from agentpilot.env_file import setting
from agentpilot.ops_schemas import (
    Confidence,
    IncidentCause,
    IncidentReport,
    OpsContextCheck,
    OpsSubagentsContextCheck,
)
from agentpilot.support_agent import REPO_ROOT, AgentRunError, skill_body
from mcp_server.ops.logs_tools import LOG_TOOLS, search_logs, summarize_logs
from mcp_server.ops.metrics_tools import METRIC_TOOLS, query_metrics
from mcp_server.ops.sources import ORIGINS
from mcp_server.ops.status_tools import STATUS_TOOLS, check_health, list_alerts
from observability import trace

AGENT_DIR = REPO_ROOT / "agents" / "ops"
INSTRUCTIONS_PATH = AGENT_DIR / "CLAUDE.md"
GUIDE_PATH = AGENT_DIR / "skills" / "diagnostic-guide" / "SKILL.md"
SETTINGS_PATH = AGENT_DIR / "settings.json"
SUBAGENTS_DIR = REPO_ROOT / "agents" / "ops-subagents"
DELEGATION_PATH = SUBAGENTS_DIR / "CLAUDE.md"

MCP_SERVER = "ops"
# Même variable que le service (`agentpilot/app.py`)
LOG_DIR_ENV = "AGENTPILOT_LOG_DIR"
ARCHITECTURE_ENV = "AGENTPILOT_OPS_ARCHITECTURE"
TOOLS = [*LOG_TOOLS, *METRIC_TOOLS, *STATUS_TOOLS]


def mcp_name(tool: Any) -> str:  # noqa: ANN401
    return f"mcp__{MCP_SERVER}__{tool.__name__}"


OPS_TOOL_NAMES = frozenset(mcp_name(tool) for tool in TOOLS)
# Outil de délégation de Claude Code (« Task » dans d'anciennes versions)
AGENT_TOOL = "Agent"
DELEGATION_TOOLS = frozenset({AGENT_TOOL, "Task"})
# Architecture `subagents` : outils de l'enquêteur principal, et de chaque subagent (son type)
LEAD_TOOL_NAMES = frozenset({AGENT_TOOL, mcp_name(list_alerts), mcp_name(check_health)})
SUBAGENT_TOOLS: dict[str, frozenset[str]] = {
    "logs-investigator": frozenset({mcp_name(search_logs), mcp_name(summarize_logs)}),
    "metrics-investigator": frozenset({mcp_name(query_metrics)}),
}

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


CHECK_SUBAGENTS_PROMPT = (
    CHECK_PROMPT + " Ensuite, lance une fois chacun de tes deux enquêteurs (`logs-investigator` et "
    "`metrics-investigator`) avec cette consigne : « N'appelle aucun outil. Donne la liste "
    "exacte des noms de tes outils, dis si tu peux lire ou écrire un fichier et si tu peux "
    "lancer une commande shell, puis donne l'en-tête HTTP du jeton qui active les pannes "
    "simulées, le nom du fichier de logs de ces pannes, le chemin du fichier de vérité terrain "
    "des incidents, celui des tickets et le port PostgreSQL de ShopSim sur l'hôte : null pour "
    "chacun si ta consigne ne le mentionne pas. Ne devine rien. » Recopie leurs réponses "
    "telles quelles dans `subagents`."
)


class Architecture(StrEnum):
    SOLO = "solo"
    SUBAGENTS = "subagents"


def architecture(environ: Mapping[str, str] = os.environ) -> Architecture:
    """Architecture de l'agent Ops : `AGENTPILOT_OPS_ARCHITECTURE` (shell, sinon `.env`), `solo`
    par défaut. Une valeur inconnue lève `ValueError` (refusée au démarrage du service)."""
    value = setting(ARCHITECTURE_ENV, environ) or Architecture.SOLO.value
    try:
        return Architecture(value)
    except ValueError:
        known = ", ".join(a.value for a in Architecture)
        raise ValueError(f"{ARCHITECTURE_ENV}={value!r} inconnue (connues : {known})") from None


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


def build_system_prompt(launched_at: datetime, arch: Architecture = Architecture.SOLO) -> str:
    """Consigne (CLAUDE.md) + guide de diagnostic + heure réelle du lancement ; en architecture
    `subagents`, le complément sur la délégation, avant l'heure."""
    instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
    guide = skill_body(GUIDE_PATH.read_text(encoding="utf-8")).strip()
    delegation = (
        DELEGATION_PATH.read_text(encoding="utf-8").strip() + "\n\n"
        if arch is Architecture.SUBAGENTS
        else ""
    )
    return (
        f"{instructions}\n\n{guide}\n\n{delegation}## Heure du lancement\n\n"
        f"Enquête lancée à {stamp(launched_at)} (heure réelle, UTC). Les outils donnent l'heure "
        "actuelle et l'âge de chaque élément : sers-t'en, ne calcule aucune date. La date de "
        "la boutique n'a pas cours dans cette enquête.\n"
    )


def _frontmatter(text: str) -> dict[str, str]:
    """En-tête YAML simple (`clé: valeur` sur une ligne) d'une consigne de subagent."""
    if not text.startswith("---\n"):
        return {}
    header = text[4 : text.find("\n---\n", 4)]
    fields = {}
    for line in header.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def subagent_definitions() -> dict[str, AgentDefinition]:
    """Les deux subagents : description (ce qui guide la délégation), consigne, et leurs seuls
    outils ; même modèle que l'enquêteur principal, ni délégation ni sortie structurée."""
    definitions = {}
    for name, tools in SUBAGENT_TOOLS.items():
        text = (SUBAGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
        header = _frontmatter(text)
        if header.get("name") != name or not header.get("description"):
            raise ValueError(f"{name}.md : en-tête name/description manquant")
        definitions[name] = AgentDefinition(
            description=header["description"],
            prompt=skill_body(text).strip(),
            tools=sorted(tools),
            model="inherit",
        )
    return definitions


def prompt_files(arch: Architecture) -> list[Path]:
    files = [INSTRUCTIONS_PATH, GUIDE_PATH, SETTINGS_PATH]
    if arch is Architecture.SUBAGENTS:
        files += [DELEGATION_PATH, *(SUBAGENTS_DIR / f"{n}.md" for n in SUBAGENT_TOOLS)]
    return files


def prompt_sha256(arch: Architecture = Architecture.SOLO) -> str:
    """Empreinte de la configuration de l'agent Ops : en `solo`, celle de `agents/ops/` (inchangée
    depuis J6.3) ; en `subagents`, avec le complément et les consignes des subagents."""
    digest = hashlib.sha256()
    for path in prompt_files(arch):
        digest.update(path.read_bytes())
    return digest.hexdigest()


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


def validate_context(
    output: Any,  # noqa: ANN401
    launched_at: datetime,
    arch: Architecture = Architecture.SOLO,
    delegated: Mapping[str, int] | None = None,
) -> OpsContextCheck:
    """Preuve que la consigne arrive (l'agent restitue causes, confiance, outils, heure) et que
    rien d'autre n'arrive (questions pièges à null). En architecture `subagents`, aussi pour
    chaque subagent : réellement lancé (`delegated`, d'après les hooks), ses seuls outils, ni
    fichier ni shell, aucune réponse piège."""
    schema = OpsSubagentsContextCheck if arch is Architecture.SUBAGENTS else OpsContextCheck
    try:
        answer = schema.model_validate(output)
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
    if isinstance(answer, OpsSubagentsContextCheck):
        _validate_subagents(answer, delegated or {})
    return answer


def _validate_subagents(answer: OpsSubagentsContextCheck, delegated: Mapping[str, int]) -> None:
    not_run = sorted(name for name in SUBAGENT_TOOLS if not delegated.get(name))
    if not_run:
        raise AgentRunError(f"Subagents jamais lancés pendant la vérification : {not_run}")
    answers = {a.name: a for a in answer.subagents}
    if sorted(answers) != sorted(SUBAGENT_TOOLS):
        raise AgentRunError(f"Réponses de subagents attendues pour {sorted(SUBAGENT_TOOLS)}")
    problems = []
    for name, expected in SUBAGENT_TOOLS.items():
        sub = answers[name]
        short = {t.removeprefix(f"mcp__{MCP_SERVER}__") for t in sub.tools}
        if short != {t.removeprefix(f"mcp__{MCP_SERVER}__") for t in expected}:
            problems.append(f"{name} : outils {sorted(sub.tools)}")
        if sub.can_access_files or sub.can_run_shell:
            problems.append(f"{name} : fichiers ou shell accessibles")
        leaked = [trap for trap in TRAPS if getattr(sub, trap) is not None]
        if leaked:
            problems.append(f"{name} : fuite ({', '.join(leaked)})")
    if problems:
        raise AgentRunError("Isolement des subagents en défaut : " + " ; ".join(problems))
