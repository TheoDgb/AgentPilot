"""Lecture ciblée de `.env` : une variable à la fois, jamais tout le fichier.

`.env` contient aussi des secrets qui ne concernent pas le service, comme `CHAOS_TOKEN`
(jamais donné aux agents). Rien n'est donc copié dans `os.environ` : la CLI Claude Code et le
serveur MCP héritent de l'environnement du service, et ne doivent jamais voir ces secrets.

Le service lit ainsi dans `.env` sa clé d'API (`support_agent.load_api_key`) et ses réglages
`AGENTPILOT_*` (`setting`), seulement s'ils ne sont pas déjà dans l'environnement. Le fichier
est relu à chaque appel : un réglage modifié s'applique sans redémarrage.
"""

import os
from collections.abc import Mapping
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
SETTINGS_PREFIX = "AGENTPILOT_"


def read_value(name: str, env_file: Path | None = None) -> str | None:
    """Valeur de la ligne `name=…` de `.env`, guillemets retirés ; None si absente ou vide."""
    path = env_file or ENV_FILE
    if not path.is_file():
        return None
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip() == name:
            value = raw.strip().strip("\"'")
    return value or None


def setting(
    name: str, environ: Mapping[str, str] = os.environ, env_file: Path | None = None
) -> str | None:
    """Réglage `AGENTPILOT_*` : l'environnement d'abord, sinon `.env` (cette ligne seulement).

    Refuse tout autre nom : ce n'est pas un moyen de lire un secret de `.env`.
    """
    if not name.startswith(SETTINGS_PREFIX):
        raise ValueError(f"{name} n'est pas un réglage {SETTINGS_PREFIX}*")
    return environ.get(name) or read_value(name, env_file)
