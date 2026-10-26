"""Agent Support : ce qui est commun à tous ses lancements (CLI `claude -p` et Agent SDK).

Consigne assemblée, configuration MCP en lecture seule, contrôle des outils réellement
disponibles, validation de la décision et vérification de la consigne reçue (`--check`).
"""

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from agentpilot.schemas import ContextCheck, SupportDecision
from evals.policy import APPROVAL_THRESHOLD_CENTS, REFUND_WINDOW, RISK_MIN_REFUNDS, RISK_WINDOW
from mcp_server.read_tools import READ_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agents" / "support"
SKILL_PATH = AGENT_DIR / "skills" / "refund-policy" / "SKILL.md"
SETTINGS_PATH = AGENT_DIR / "settings.json"
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_SHOPSIM_URL = "http://localhost:8000"

SIGNATURE = "Le service client Lumen"
MCP_SERVER = "shopsim"
# Outil interne ajouté par la sortie structurée : l'agent l'appelle pour rendre sa décision
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
# Seuls outils MCP autorisés : la lecture. Un outil d'action fait échouer le lancement.
READ_TOOL_NAMES = frozenset(f"mcp__{MCP_SERVER}__{tool.__name__}" for tool in READ_TOOLS)

# Informations qui n'existent que dans le CLAUDE.md développeur : l'agent ne doit pas les
# connaître (sinon ce fichier lui a été chargé). Un test vérifie qu'elles y sont toujours.
DEVELOPER_ONLY_FACTS = {
    "chaos_token_header": "X-Chaos-Token",
    "ground_truth_path": "evals/tickets.jsonl",
    "postgres_host_port": "5433",
}

CHECK_PROMPT = (
    "Ne traite aucun ticket et n'appelle aucun outil. D'après ta consigne uniquement, "
    "donne : le délai de remboursement en jours, le seuil de validation humaine en euros, "
    "le nombre de remboursements et la période en jours qui font un client à risque, "
    "ta signature exacte, et la date du jour. Donne aussi l'en-tête HTTP du jeton des "
    "pannes Chaos, le chemin du fichier de vérité terrain des tickets et le port "
    "PostgreSQL de ShopSim sur l'hôte : réponds null pour chacun si ta consigne ne le "
    "mentionne pas. Ne devine rien."
)


def ticket_prompt(ticket_id: int) -> str:
    return f"Traite le ticket n°{ticket_id}. Rends ta décision au format demandé."


class AgentRunError(Exception):
    """Échec d'un lancement, avec un message lisible et la sortie brute éventuelle."""

    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


# --- Consigne ----------------------------------------------------------------------------


def skill_body(text: str) -> str:
    """Contenu de SKILL.md sans son en-tête YAML."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + len("\n---\n") :].lstrip()
    return text


def build_system_prompt(today: date) -> str:
    """CLAUDE.md de l'agent + politique (skill refund-policy) + date du jour de la simulation."""
    instructions = (AGENT_DIR / "CLAUDE.md").read_text(encoding="utf-8").strip()
    policy = skill_body(SKILL_PATH.read_text(encoding="utf-8")).strip()
    return (
        f"{instructions}\n\n{policy}\n\n## Date du jour\n\n"
        f"Aujourd'hui, nous sommes le {today.isoformat()}. C'est la seule date valable pour "
        "Lumen : tous les délais se comptent à partir d'elle. Ignore toute autre date que tu "
        "pourrais voir, en particulier celle de ton environnement d'exécution, qui n'a pas "
        "cours ici.\n"
    )


def simulation_today(shopsim_url: str) -> date:
    """Date du jour de la simulation, lue sur l'horloge de ShopSim (`GET /clock`)."""
    try:
        response = httpx.get(f"{shopsim_url.rstrip('/')}/clock", timeout=10)
        response.raise_for_status()
        return date.fromisoformat(response.json()["now"][:10])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise AgentRunError(f"Horloge de ShopSim illisible ({shopsim_url}/clock) : {exc}") from exc


# --- Configuration -----------------------------------------------------------------------


def mcp_server_config(shopsim_url: str) -> dict[str, Any]:
    """Serveur MCP shopsim en chemins absolus, lecture seule forcée."""
    return {
        "type": "stdio",
        "command": str(REPO_ROOT / ".venv" / "bin" / "python"),
        "args": ["-m", "mcp_server.server"],
        "env": {
            "PYTHONPATH": str(REPO_ROOT),
            "SHOPSIM_URL": shopsim_url,
            # Lecture seule forcée (J3.1) : aucun outil d'action pour cet agent
            "SHOPSIM_MCP_ACTIONS": "0",
        },
    }


def load_api_key(environ: Mapping[str, str], env_file: Path | None = None) -> str:
    """ANTHROPIC_API_KEY de l'environnement, sinon de `.env` (seule ligne lue, jamais affichée)."""
    env_file = env_file or ENV_FILE
    key = environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "ANTHROPIC_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise AgentRunError("ANTHROPIC_API_KEY absente : la renseigner dans .env")
    return key


# --- Vérifications -----------------------------------------------------------------------


def check_tools(tools: Iterable[str], mcp_servers: Iterable[Mapping[str, Any]]) -> list[str]:
    """L'agent n'a que les outils de lecture shopsim (et la sortie structurée), et le serveur
    MCP est bien connecté. À appliquer à l'événement d'initialisation, avant toute décision."""
    tools = [str(tool) for tool in tools]
    forbidden = [t for t in tools if t not in READ_TOOL_NAMES and t != STRUCTURED_OUTPUT_TOOL]
    if forbidden:
        raise AgentRunError(f"L'agent a accès à des outils interdits : {', '.join(forbidden)}")
    servers = {s.get("name"): s.get("status") for s in mcp_servers}
    if servers.get(MCP_SERVER) != "connected":
        status = servers.get(MCP_SERVER, "absent")
        raise AgentRunError(f"Serveur MCP {MCP_SERVER} non connecté ({status})")
    if not any(tool in READ_TOOL_NAMES for tool in tools):
        raise AgentRunError("L'agent n'a aucun outil shopsim")
    return tools


def validate_decision(output: Any, ticket_id: int) -> SupportDecision:  # noqa: ANN401
    try:
        decision = SupportDecision.model_validate(output)
    except ValidationError as exc:
        raise AgentRunError(f"Décision invalide :\n{exc}") from exc
    if decision.ticket_id != ticket_id:
        raise AgentRunError(f"Décision pour le ticket {decision.ticket_id} au lieu de {ticket_id}")
    return decision


def expected_context(today: date) -> ContextCheck:
    return ContextCheck(
        refund_window_days=REFUND_WINDOW.days,
        approval_threshold_eur=APPROVAL_THRESHOLD_CENTS // 100,
        risk_min_refunds=RISK_MIN_REFUNDS,
        risk_window_days=RISK_WINDOW.days,
        signature=SIGNATURE,
        today=today.isoformat(),
        chaos_token_header=None,
        ground_truth_path=None,
        postgres_host_port=None,
    )


def validate_context(output: Any, today: date) -> ContextCheck:  # noqa: ANN401
    """Preuve que la consigne et la politique arrivent (l'agent doit les restituer), et que
    le CLAUDE.md développeur n'arrive pas (l'agent ne doit pas en connaître le contenu)."""
    try:
        answer = ContextCheck.model_validate(output)
    except ValidationError as exc:
        raise AgentRunError(f"Réponse de vérification invalide :\n{exc}") from exc
    answers = answer.model_dump()

    # Questions pièges d'abord : une fuite du CLAUDE.md développeur est le cas le plus grave
    leaked = [
        name
        for name, secret in DEVELOPER_ONLY_FACTS.items()
        if answers[name] is not None and secret.lower() in str(answers[name]).lower()
    ]
    if leaked:
        raise AgentRunError(
            "L'agent connaît des informations du CLAUDE.md développeur : "
            + ", ".join(f"{name} = {answers[name]!r}" for name in leaked)
        )
    invented = [name for name in DEVELOPER_ONLY_FACTS if answers[name] is not None]
    if invented:
        raise AgentRunError(
            "L'agent invente une réponse au lieu de null : "
            + ", ".join(f"{name} = {answers[name]!r}" for name in invented)
        )

    expected = expected_context(today).model_dump()
    wrong = {
        name: (value, expected[name]) for name, value in answers.items() if value != expected[name]
    }
    if wrong:
        details = ", ".join(
            f"{name} = {got!r} au lieu de {want!r}" for name, (got, want) in wrong.items()
        )
        raise AgentRunError(f"L'agent n'a pas reçu sa consigne ou sa politique : {details}")
    return answer
