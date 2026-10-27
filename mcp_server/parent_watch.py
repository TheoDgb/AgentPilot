"""Arrêt d'un processus enfant avec son parent, même si le parent est tué brutalement.

Un agent orphelin (CLI Claude Code ou serveur MCP) pourrait continuer à agir pendant qu'une
relance traite le même ticket : ces deux fonctions l'empêchent.

- `die_with_parent` demande au noyau Linux (`PR_SET_PDEATHSIG`) d'envoyer un signal au
  processus dès que son parent meurt, même par SIGKILL. Le réglage survit à `exec`. Il est
  lié au fil d'exécution qui a créé le processus : on ne l'emploie que pour la CLI, lancée
  par la boucle d'événements (fil principal) du service.
- `exit_with_parent` surveille le parent depuis un fil de fond (toutes les secondes) : il ne
  dépend pas du fil qui a créé le processus, ni de Linux. C'est le choix du serveur MCP, lancé
  par la CLI dont on ne connaît pas les fils.

Bibliothèque standard seulement : le lanceur de la CLI s'en sert avant même `exec`.
"""

import ctypes
import os
import signal
import sys
import threading
import time

PR_SET_PDEATHSIG = 1
WATCH_INTERVAL_S = 1.0


def die_with_parent(parent: int | None = None, sig: int = signal.SIGKILL) -> None:
    """Le noyau envoie `sig` au processus quand son parent meurt (Linux ; ailleurs, rien).
    `parent` : PID attendu du parent (par défaut, le parent actuel)."""
    if not sys.platform.startswith("linux"):
        return
    parent = os.getppid() if parent is None else parent
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"prctl(PR_SET_PDEATHSIG) : {os.strerror(error)}")
    # Parent déjà mort (avant ou pendant le réglage) : le signal ne viendrait jamais
    if os.getppid() != parent:
        os.kill(os.getpid(), sig)


def exit_with_parent(parent: int | None = None, interval_s: float = WATCH_INTERVAL_S) -> None:
    """Quitte le processus (sans nettoyage) dès que son parent a disparu. `parent` : PID lu au
    plus tôt au démarrage (par défaut, le parent actuel)."""
    parent = os.getppid() if parent is None else parent

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval_s)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()
