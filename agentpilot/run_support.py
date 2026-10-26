"""Lance l'agent Support en headless (`claude -p`) sur un ticket et enregistre sa décision.

Usage :
    python -m agentpilot.run_support 36          # décision sur le ticket 36
    python -m agentpilot.run_support --check     # preuve que l'agent a reçu sa consigne

L'agent est isolé : `--bare` ignore le CLAUDE.md du dépôt, les hooks, plugins, skills et la
mémoire ; sa consigne et sa politique lui sont passées explicitement ; il n'a que les outils
en lecture du serveur MCP shopsim. Chaque sortie est vérifiée (outils réellement disponibles,
schéma Pydantic) avant d'être enregistrée dans `runs/support/`.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from agentpilot.schemas import ContextCheck, SupportDecision
from evals.policy import APPROVAL_THRESHOLD_CENTS, REFUND_WINDOW, RISK_MIN_REFUNDS, RISK_WINDOW
from mcp_server.read_tools import READ_TOOLS
from shopsim.app.clock import SIMULATION_NOW

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agents" / "support"
SKILL_PATH = AGENT_DIR / "skills" / "refund-policy" / "SKILL.md"
SETTINGS_PATH = AGENT_DIR / "settings.json"
RUNS_DIR = REPO_ROOT / "runs" / "support"
ENV_FILE = REPO_ROOT / ".env"

SIGNATURE = "Le service client Lumen"
MCP_SERVER = "shopsim"
ALLOWED_TOOLS = f"mcp__{MCP_SERVER}__*"
# Outil interne ajouté par `--json-schema` : l'agent l'appelle pour rendre sa sortie structurée
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
# Seuls outils MCP autorisés : la lecture. Un outil d'action fait échouer le lancement.
READ_TOOL_NAMES = frozenset(f"mcp__{MCP_SERVER}__{tool.__name__}" for tool in READ_TOOLS)
DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TURNS = 15
TIMEOUT_S = 300
# Seules variables transmises à `claude` (et donc au serveur MCP) : jamais CHAOS_TOKEN
PASSED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


class RunError(Exception):
    """Échec d'un lancement, avec un message lisible et la sortie brute de `claude`."""

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


def build_system_prompt(today: datetime = SIMULATION_NOW) -> str:
    """CLAUDE.md de l'agent + politique (skill refund-policy) + date du jour."""
    instructions = (AGENT_DIR / "CLAUDE.md").read_text(encoding="utf-8").strip()
    policy = skill_body(SKILL_PATH.read_text(encoding="utf-8")).strip()
    date = today.date().isoformat()
    return (
        f"{instructions}\n\n{policy}\n\n## Date du jour\n\n"
        f"Aujourd'hui, nous sommes le {date}. C'est la seule date valable pour Lumen : tous "
        "les délais se comptent à partir d'elle. Ignore toute autre date que tu pourrais voir, "
        "en particulier celle de ton environnement d'exécution, qui n'a pas cours ici.\n"
    )


# --- Lancement ---------------------------------------------------------------------------


def mcp_config(shopsim_url: str) -> dict[str, Any]:
    """Chemins absolus : le serveur démarre quel que soit le dossier courant de `claude`."""
    return {
        "mcpServers": {
            MCP_SERVER: {
                "command": str(REPO_ROOT / ".venv" / "bin" / "python"),
                "args": ["-m", "mcp_server.server"],
                "env": {
                    "PYTHONPATH": str(REPO_ROOT),
                    "SHOPSIM_URL": shopsim_url,
                    # Lecture seule forcée (J3.1) : aucun outil d'action pour cet agent
                    "SHOPSIM_MCP_ACTIONS": "0",
                },
            }
        }
    }


def load_api_key(environ: Mapping[str, str], env_file: Path = ENV_FILE) -> str:
    """ANTHROPIC_API_KEY de l'environnement, sinon de `.env` (jamais affichée)."""
    key = environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "ANTHROPIC_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise RunError("ANTHROPIC_API_KEY absente : la renseigner dans .env (requise par --bare)")
    return key


def claude_env(environ: Mapping[str, str], api_key: str) -> dict[str, str]:
    env = {name: environ[name] for name in PASSED_ENV if name in environ}
    env["ANTHROPIC_API_KEY"] = api_key
    return env


def build_command(
    prompt: str, schema: type[BaseModel], model: str, max_turns: int, shopsim_url: str
) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--bare",
        # Aucun outil intégré (fichiers, shell, web) : seuls restent les outils MCP
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp_config(shopsim_url)),
        "--settings",
        str(SETTINGS_PATH),
        "--allowedTools",
        ALLOWED_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        build_system_prompt(),
        "--json-schema",
        json.dumps(schema.model_json_schema()),
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--no-session-persistence",
    ]


def run_claude(command: list[str], env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=AGENT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RunError("Commande `claude` introuvable : Claude Code est-il installé ?") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunError(
            f"L'agent n'a pas répondu en {TIMEOUT_S} s",
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        ) from exc
    if not completed.stdout.strip():
        raise RunError(
            f"`claude` s'est arrêté sans sortie (code {completed.returncode})",
            stderr=completed.stderr,
        )
    return completed.stdout


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


# --- Vérification de la sortie -----------------------------------------------------------


def parse_events(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Événements `system/init` et `result` du flux stream-json."""
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
    if init is None:
        raise RunError("Sortie de `claude` sans événement d'initialisation", stdout=stdout)
    if result is None:
        raise RunError("Sortie de `claude` sans résultat final", stdout=stdout)
    return init, result


def check_init(init: dict[str, Any]) -> list[str]:
    """L'agent n'a que les outils de lecture shopsim (et la sortie structurée), et le serveur
    MCP est bien connecté."""
    tools = [str(tool) for tool in init.get("tools", [])]
    forbidden = [t for t in tools if t not in READ_TOOL_NAMES and t != STRUCTURED_OUTPUT_TOOL]
    if forbidden:
        raise RunError(f"L'agent a accès à des outils interdits : {', '.join(forbidden)}")
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    if servers.get(MCP_SERVER) != "connected":
        status = servers.get(MCP_SERVER, "absent")
        raise RunError(f"Serveur MCP {MCP_SERVER} non connecté ({status})")
    if not any(tool in READ_TOOL_NAMES for tool in tools):
        raise RunError("L'agent n'a aucun outil shopsim")
    return tools


def structured_output(result: dict[str, Any]) -> Any:  # noqa: ANN401
    if result.get("is_error") or result.get("subtype") != "success":
        detail = result.get("result") or result.get("subtype")
        raise RunError(f"L'agent a échoué : {detail}")
    if "structured_output" not in result:
        raise RunError("L'agent n'a pas rendu de sortie structurée")
    return result["structured_output"]


def run_metadata(init: dict[str, Any], result: dict[str, Any], tools: list[str]) -> dict[str, Any]:
    return {
        "session_id": result.get("session_id"),
        "model": init.get("model"),
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "total_cost_usd": result.get("total_cost_usd"),
        "tools": tools,
    }


# --- Commandes ---------------------------------------------------------------------------


def _launch(
    prompt: str,
    schema: type[BaseModel],
    model: str,
    max_turns: int,
    environ: Mapping[str, str],
) -> tuple[Any, dict[str, Any], str]:
    api_key = load_api_key(environ)
    shopsim_url = environ.get("SHOPSIM_URL", "http://localhost:8000")
    command = build_command(prompt, schema, model, max_turns, shopsim_url)
    stdout = run_claude(command, claude_env(environ, api_key))
    try:
        init, result = parse_events(stdout)
        tools = check_init(init)
        output = structured_output(result)
    except RunError as exc:
        exc.stdout = exc.stdout or stdout
        raise
    return output, run_metadata(init, result, tools), stdout


def decide_ticket(
    ticket_id: int,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    environ: Mapping[str, str] = os.environ,
) -> tuple[SupportDecision, dict[str, Any]]:
    prompt = f"Traite le ticket n°{ticket_id}. Rends ta décision au format demandé."
    output, metadata, stdout = _launch(prompt, SupportDecision, model, max_turns, environ)
    try:
        decision = SupportDecision.model_validate(output)
    except ValidationError as exc:
        raise RunError(f"Décision invalide :\n{exc}", stdout=stdout) from exc
    if decision.ticket_id != ticket_id:
        raise RunError(
            f"Décision pour le ticket {decision.ticket_id} au lieu de {ticket_id}", stdout=stdout
        )
    return decision, metadata


# Informations qui n'existent que dans le CLAUDE.md développeur : l'agent ne doit pas les
# connaître (sinon ce fichier lui a été chargé). Un test vérifie qu'elles y sont toujours.
DEVELOPER_ONLY_FACTS = {
    "chaos_token_header": "X-Chaos-Token",
    "ground_truth_path": "evals/tickets.jsonl",
    "postgres_host_port": "5433",
}


def expected_context() -> ContextCheck:
    return ContextCheck(
        refund_window_days=REFUND_WINDOW.days,
        approval_threshold_eur=APPROVAL_THRESHOLD_CENTS // 100,
        risk_min_refunds=RISK_MIN_REFUNDS,
        risk_window_days=RISK_WINDOW.days,
        signature=SIGNATURE,
        today=SIMULATION_NOW.date().isoformat(),
        chaos_token_header=None,
        ground_truth_path=None,
        postgres_host_port=None,
    )


def check_context(
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    environ: Mapping[str, str] = os.environ,
) -> tuple[ContextCheck, dict[str, Any]]:
    """Preuve que la consigne et la politique arrivent (l'agent doit les restituer), et que
    le CLAUDE.md développeur n'arrive pas (l'agent ne doit pas en connaître le contenu)."""
    prompt = (
        "Ne traite aucun ticket et n'appelle aucun outil. D'après ta consigne uniquement, "
        "donne : le délai de remboursement en jours, le seuil de validation humaine en euros, "
        "le nombre de remboursements et la période en jours qui font un client à risque, "
        "ta signature exacte, et la date du jour. Donne aussi l'en-tête HTTP du jeton des "
        "pannes Chaos, le chemin du fichier de vérité terrain des tickets et le port "
        "PostgreSQL de ShopSim sur l'hôte : réponds null pour chacun si ta consigne ne le "
        "mentionne pas. Ne devine rien."
    )
    output, metadata, stdout = _launch(prompt, ContextCheck, model, max_turns, environ)
    try:
        answer = ContextCheck.model_validate(output)
    except ValidationError as exc:
        raise RunError(f"Réponse de vérification invalide :\n{exc}", stdout=stdout) from exc
    answers = answer.model_dump()

    # Questions pièges d'abord : une fuite du CLAUDE.md développeur est le cas le plus grave
    leaked = [
        name
        for name, secret in DEVELOPER_ONLY_FACTS.items()
        if answers[name] is not None and secret.lower() in str(answers[name]).lower()
    ]
    if leaked:
        raise RunError(
            "L'agent connaît des informations du CLAUDE.md développeur : "
            + ", ".join(f"{name} = {answers[name]!r}" for name in leaked),
            stdout=stdout,
        )
    invented = [name for name in DEVELOPER_ONLY_FACTS if answers[name] is not None]
    if invented:
        raise RunError(
            "L'agent invente une réponse au lieu de null : "
            + ", ".join(f"{name} = {answers[name]!r}" for name in invented),
            stdout=stdout,
        )

    expected = expected_context().model_dump()
    wrong = {
        name: (value, expected[name]) for name, value in answers.items() if value != expected[name]
    }
    if wrong:
        details = ", ".join(
            f"{name} = {got!r} au lieu de {want!r}" for name, (got, want) in wrong.items()
        )
        raise RunError(
            f"L'agent n'a pas reçu sa consigne ou sa politique : {details}", stdout=stdout
        )
    return answer, metadata


def _save(name: str, payload: dict[str, Any], runs_dir: Path, suffix: str = ".json") -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}{suffix}"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None, runs_dir: Path = RUNS_DIR) -> int:
    parser = argparse.ArgumentParser(description="Lance l'agent Support sur un ticket.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("ticket_id", nargs="?", type=int, help="Numéro du ticket")
    target.add_argument("--check", action="store_true", help="Vérifie la consigne reçue")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    args = parser.parse_args(argv)

    name = "check" if args.check else f"ticket-{args.ticket_id}"
    try:
        if args.check:
            answer, metadata = check_context(args.model, args.max_turns)
            payload = {"check": answer.model_dump(mode="json"), "run": metadata}
        else:
            decision, metadata = decide_ticket(args.ticket_id, args.model, args.max_turns)
            payload = {
                "ticket_id": args.ticket_id,
                "decision": decision.model_dump(mode="json"),
                "run": metadata,
            }
    except RunError as exc:
        error = {"error": str(exc), "stdout": exc.stdout, "stderr": exc.stderr}
        path = _save(name, error, runs_dir, suffix=".error.json")
        print(f"Échec : {exc}\nSortie brute : {path}", file=sys.stderr)
        return 1

    path = _save(name, payload, runs_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Enregistré : {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
