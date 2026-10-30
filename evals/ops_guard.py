"""Gardien de l'évaluation de l'agent Ops : nettoie si le lanceur meurt brutalement (`kill -9`).

Lancé par `evals.ops_run` dans sa propre session (un Ctrl+C du lanceur ne le touche pas). Il
lit sur son entrée standard l'état que le lanceur lui envoie, une ligne JSON par changement :
processus lancés (AgentPilot, générateur de trafic, charge de remboursements), fausse alerte
active. Le lanceur termine par `{"done": true}` après avoir tout nettoyé lui-même.

Entrée fermée sans `done` (lanceur mort) : nettoyage, **dans cet ordre** :
1. arrêt de la charge de remboursements, et attente de sa fin (l'envoi en cours va au bout) ;
2. pannes désactivées (seulement ensuite : un remboursement parti après la panne réussirait) ;
3. fausse alerte résolue ;
4. arrêt du générateur de trafic, puis d'AgentPilot.
"""

import json
import os
import signal
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from shopsim import chaos_cli

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
GRACE_S = 30.0


@dataclass
class GuardState:
    service: int | None = None
    traffic: int | None = None
    load: int | None = None
    fake_alert: dict[str, Any] | None = None
    shopsim_url: str = "http://localhost:8000"
    agentpilot_url: str = "http://localhost:8001"
    done: bool = False
    steps: list[str] = field(default_factory=list)


def read_state(lines: Iterable[str]) -> GuardState:
    """État final d'après les lignes reçues, jusqu'à la fermeture de l'entrée."""
    state = GuardState()
    for line in lines:
        try:
            update = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key, value in update.items():
            if hasattr(state, key) and key != "steps":
                setattr(state, key, value)
    return state


def _alive(pid: int) -> bool:
    """Processus encore en marche ; un zombie (fini, pas encore recueilli) compte comme fini."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def stop_pid(pid: int, grace_s: float = GRACE_S) -> None:
    """SIGTERM, attente de la fin (le processus finit ce qu'il fait), SIGKILL au-delà."""
    if not _alive(pid):
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)


def faults_off(shopsim_url: str) -> None:
    token = chaos_cli.read_token(os.environ, ENV_FILE)
    if token:
        chaos_cli.turn_off(chaos_cli.http_sender(shopsim_url), token, chaos_cli.ALL)


def resolve_alert(agentpilot_url: str, payload: dict[str, Any]) -> None:
    httpx.post(f"{agentpilot_url}/alerts/webhook", json=payload, timeout=10)


def cleanup(
    state: GuardState,
    *,
    stop: Callable[[int], None] = stop_pid,
    disable_faults: Callable[[str], None] = faults_off,
    resolve: Callable[[str, dict[str, Any]], None] = resolve_alert,
) -> list[str]:
    """Nettoyage après la mort du lanceur ; renvoie les étapes faites, dans l'ordre."""
    steps = []

    def attempt(name: str, action: Callable[[], None]) -> None:
        # Une étape qui échoue n'empêche jamais les suivantes
        try:
            action()
            steps.append(name)
        except Exception:  # noqa: BLE001
            steps.append(f"{name} (échec)")

    load, alert, traffic, service = state.load, state.fake_alert, state.traffic, state.service
    if load is not None:
        attempt("load", lambda: stop(load))
    attempt("faults", lambda: disable_faults(state.shopsim_url))
    if alert is not None:
        attempt("alert", lambda: resolve(state.agentpilot_url, alert))
    if traffic is not None:
        attempt("traffic", lambda: stop(traffic))
    if service is not None:
        attempt("service", lambda: stop(service))
    return steps


def main() -> int:
    # Propre session : ni Ctrl+C ni SIGHUP du terminal du lanceur
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    state = read_state(sys.stdin)
    if state.done:
        return 0
    cleanup(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
