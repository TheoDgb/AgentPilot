"""Pilotage des pannes Chaos de ShopSim depuis le terminal, sans curl.

    python -m shopsim.chaos_cli on payment_latency
    python -m shopsim.chaos_cli off payment_latency
    python -m shopsim.chaos_cli off all
    python -m shopsim.chaos_cli status

Jeton : `CHAOS_TOKEN` de l'environnement, sinon la seule ligne `CHAOS_TOKEN=` de `.env` (le
reste du fichier n'est jamais lu ni chargé). Le jeton n'est jamais affiché. ShopSim est joint à
`SHOPSIM_URL` (`http://localhost:8000` par défaut).

Outil d'humain : jamais donné aux agents, comme le jeton.

Bibliothèque standard seulement (urllib), comme le reste de ShopSim : aucune dépendance de plus.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from shopsim.app.models import ChaosScenario

TOKEN_ENV = "CHAOS_TOKEN"
URL_ENV = "SHOPSIM_URL"
DEFAULT_URL = "http://localhost:8000"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
TIMEOUT_S = 10.0
ALL = "all"

DESCRIPTIONS = {
    ChaosScenario.PAYMENT_LATENCY: "Paiements répond en 4 s au lieu de 100 ms",
    ChaosScenario.DB_ERRORS: "les lectures des commandes renvoient 500",
    ChaosScenario.CONNECTION_SATURATION: "les requêtes ralentissent (0 à 5 s en 10 min)",
    ChaosScenario.BAD_DEPLOY: "seuls les remboursements échouent (500)",
}

# Envoie une requête à ShopSim : (méthode, chemin, jeton) → (code HTTP, corps JSON ou None)
Send = Callable[[str, str, str], tuple[int, Any]]


class ChaosError(Exception):
    """Échec affiché tel quel, sans trace."""


def read_token(environ: Mapping[str, str], env_file: Path) -> str | None:
    """`CHAOS_TOKEN` de l'environnement, sinon de sa seule ligne dans `.env`."""
    if environ.get(TOKEN_ENV):
        return environ[TOKEN_ENV]
    if not env_file.is_file():
        return None
    value = None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip() == TOKEN_ENV:
            value = raw.strip().strip("\"'")
    return value or None


def http_sender(base_url: str) -> Send:
    def send(method: str, path: str, token: str) -> tuple[int, Any]:
        request = urllib.request.Request(
            base_url.rstrip("/") + path, method=method, headers={"X-Chaos-Token": token}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ChaosError(f"ShopSim injoignable à {base_url} ({reason}).") from None
        return status, json.loads(body) if body else None

    return send


def _check(status: int, body: Any) -> None:  # noqa: ANN401
    if status == 403:
        raise ChaosError(
            "Jeton refusé par ShopSim : CHAOS_TOKEN de .env doit être celui donné au conteneur "
            "(redémarrer ShopSim après l'avoir changé : docker compose up -d app)."
        )
    if status >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        raise ChaosError(f"ShopSim a répondu {status} : {detail}")


def status(send: Send, token: str) -> list[str]:
    code, body = send("GET", "/chaos", token)
    _check(code, body)
    active = body["active"]
    if not active:
        return ["Aucune panne active."]
    return [
        f"{fault['scenario']} active depuis {fault['activated_at']} (heure simulée) : "
        f"{DESCRIPTIONS.get(ChaosScenario(fault['scenario']), '')}"
        for fault in active
    ]


def turn_on(send: Send, token: str, scenario: ChaosScenario) -> list[str]:
    code, body = send("POST", f"/chaos/{scenario}", token)
    _check(code, body)
    if code == 200:
        return [f"{scenario} était déjà active : rien ne change."]
    return [f"{scenario} activée : {DESCRIPTIONS[scenario]}."]


def turn_off(send: Send, token: str, target: str) -> list[str]:
    scenarios = list(ChaosScenario) if target == ALL else [ChaosScenario(target)]
    for scenario in scenarios:
        code, body = send("DELETE", f"/chaos/{scenario}", token)
        _check(code, body)
    if target == ALL:
        return ["Toutes les pannes sont arrêtées."]
    return [f"{target} arrêtée (ou déjà inactive)."]


def parser() -> argparse.ArgumentParser:
    scenarios = [s.value for s in ChaosScenario]
    main_parser = argparse.ArgumentParser(
        prog="python -m shopsim.chaos_cli",
        description="Active, arrête et liste les pannes Chaos de ShopSim.",
    )
    commands = main_parser.add_subparsers(dest="command", required=True)
    on = commands.add_parser("on", help="active une panne")
    on.add_argument("scenario", choices=scenarios)
    off = commands.add_parser("off", help="arrête une panne, ou toutes (all)")
    off.add_argument("scenario", choices=[*scenarios, ALL])
    commands.add_parser("status", help="liste les pannes actives")
    return main_parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] = os.environ,
    env_file: Path = ENV_FILE,
    send: Send | None = None,
) -> int:
    args = parser().parse_args(argv)
    token = read_token(environ, env_file)
    if token is None:
        print(f"CHAOS_TOKEN absent : l'ajouter à {env_file} (vide = Chaos fermé).", file=sys.stderr)
        return 1
    send = send or http_sender(environ.get(URL_ENV) or DEFAULT_URL)
    try:
        if args.command == "on":
            lines = turn_on(send, token, ChaosScenario(args.scenario))
        elif args.command == "off":
            lines = turn_off(send, token, args.scenario)
        else:
            lines = status(send, token)
    except ChaosError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
