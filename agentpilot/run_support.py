"""Lance l'agent Support en headless (`claude -p`) sur un ticket et enregistre sa décision.

Usage :
    python -m agentpilot.run_support 36          # décision sur le ticket 36
    python -m agentpilot.run_support --check     # preuve que l'agent a reçu sa consigne

L'agent est isolé : `--bare` ignore le CLAUDE.md du dépôt, les hooks, plugins, skills et la
mémoire ; sa consigne et sa politique lui sont passées explicitement ; il n'a que les outils
en lecture du serveur MCP shopsim. La CLI n'a pas les hooks de validation : elle reste donc
en lecture seule et ne fait que proposer une décision. Chaque sortie est vérifiée (outils
réellement disponibles, schéma Pydantic) avant d'être enregistrée dans `runs/support/`.

Le service AgentPilot lance le même agent par l'Agent SDK (`agentpilot/sdk_runner.py`),
avec les outils d'action sous hooks ; le code commun est dans `agentpilot/support_agent.py`.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentpilot import support_agent
from agentpilot.schemas import ContextCheck, SupportDecision
from agentpilot.support_agent import (
    AGENT_DIR,
    CHECK_PROMPT,
    DEFAULT_SHOPSIM_URL,
    MCP_SERVER,
    REPO_ROOT,
    SETTINGS_PATH,
    build_system_prompt,
    load_api_key,
)
from agentpilot.support_agent import AgentRunError as RunError

RUNS_DIR = REPO_ROOT / "runs" / "support"
ALLOWED_TOOLS = f"mcp__{MCP_SERVER}__*"
DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TURNS = 15
TIMEOUT_S = 300
# Seules variables transmises à `claude` (et donc au serveur MCP) : jamais CHAOS_TOKEN
PASSED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


# --- Lancement ---------------------------------------------------------------------------


def mcp_config(shopsim_url: str) -> dict[str, Any]:
    return {"mcpServers": {MCP_SERVER: support_agent.mcp_server_config(shopsim_url)}}


def claude_env(environ: Mapping[str, str], api_key: str) -> dict[str, str]:
    env = {name: environ[name] for name in PASSED_ENV if name in environ}
    env["ANTHROPIC_API_KEY"] = api_key
    return env


def build_command(
    prompt: str,
    schema: type[BaseModel],
    model: str,
    max_turns: int,
    shopsim_url: str,
    today: date,
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
        build_system_prompt(today),
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
    return support_agent.check_tools(init.get("tools", []), init.get("mcp_servers", []))


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
) -> tuple[Any, dict[str, Any], str, date]:
    api_key = load_api_key(environ)
    shopsim_url = environ.get("SHOPSIM_URL", DEFAULT_SHOPSIM_URL)
    # Date du jour lue sur l'horloge de ShopSim : la même que celle des outils
    today = support_agent.simulation_today(shopsim_url)
    command = build_command(prompt, schema, model, max_turns, shopsim_url, today)
    stdout = run_claude(command, claude_env(environ, api_key))
    try:
        init, result = parse_events(stdout)
        tools = check_init(init)
        output = structured_output(result)
    except RunError as exc:
        exc.stdout = exc.stdout or stdout
        raise
    return output, run_metadata(init, result, tools), stdout, today


def decide_ticket(
    ticket_id: int,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    environ: Mapping[str, str] = os.environ,
) -> tuple[SupportDecision, dict[str, Any]]:
    prompt = support_agent.ticket_prompt(ticket_id)
    output, metadata, stdout, _ = _launch(prompt, SupportDecision, model, max_turns, environ)
    try:
        decision = support_agent.validate_decision(output, ticket_id)
    except RunError as exc:
        exc.stdout = stdout
        raise
    return decision, metadata


def check_context(
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    environ: Mapping[str, str] = os.environ,
) -> tuple[ContextCheck, dict[str, Any]]:
    output, metadata, stdout, today = _launch(CHECK_PROMPT, ContextCheck, model, max_turns, environ)
    try:
        answer = support_agent.validate_context(output, today)
    except RunError as exc:
        exc.stdout = stdout
        raise
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
