"""Lanceur de la CLI Claude Code pour le service : la CLI meurt avec le service.

Le SDK lance la CLI en sous-processus et l'arrête proprement quand l'exécution se termine.
Mais si le service est tué brutalement (SIGKILL, plantage), rien n'arrête la CLI : un agent
orphelin pourrait continuer à agir pendant qu'une relance traite le même ticket.

`ClaudeAgentOptions.cli_path` pointe donc vers un petit script (écrit dans le dossier
temporaire de l'exécution, voir `write_launcher`) qui appelle `main` : il demande au noyau
de tuer le processus à la mort de son parent (`die_with_parent`), puis se remplace par la
vraie CLI (`exec` : même PID, le SDK le gère comme d'habitude).
"""

import os
import shutil
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from agentpilot.env_file import setting
from mcp_server.parent_watch import die_with_parent

# Chemin de la vraie CLI, transmis au lanceur ; réglable pour utiliser une autre CLI
CLI_ENV = "AGENTPILOT_CLAUDE_CLI"
# PID du service, transmis au lanceur : s'il est déjà mort au lancement, la CLI ne démarre pas
SERVICE_PID_ENV = "AGENTPILOT_SERVICE_PID"


def real_cli(environ: Mapping[str, str]) -> str | None:
    """CLI à lancer : `AGENTPILOT_CLAUDE_CLI` (environnement, sinon `.env`), sinon celle du SDK,
    sinon `claude` du PATH."""
    configured = setting(CLI_ENV, environ)
    if configured:
        return configured
    # Import ici : le lanceur, qui importe ce module à chaque exécution, reste léger
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("claude")


def write_launcher(directory: Path) -> Path:
    """Script exécutable qui lance `main` avec l'interpréteur du service."""
    launcher = directory / "claude-guard"
    launcher.write_text(
        f"#!{sys.executable}\nfrom agentpilot.claude_guard import main\n\nmain()\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def main() -> NoReturn:
    cli = os.environ.get(CLI_ENV)
    if not cli:
        sys.exit(f"claude-guard : {CLI_ENV} manquant")
    service = os.environ.get(SERVICE_PID_ENV)
    die_with_parent(int(service) if service else None)
    os.execv(cli, [cli, *sys.argv[1:]])
