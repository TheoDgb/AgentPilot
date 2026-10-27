"""Remise à zéro complète : ShopSim et AgentPilot repartent du même état (évaluations, J5).

    python -m agentpilot.reset          # demande confirmation
    python -m agentpilot.reset --yes    # sans question (scripts, CI)

1. Refuse de tourner si le service AgentPilot est lancé : il tient le verrou de la base
   (`agentpilot.db.service_lock`). La remise à zéro le tient à son tour jusqu'à la fin, pour
   qu'aucun service ne démarre au milieu.
2. Relance le seed de ShopSim (`shopsim/seed.py`) : données de démo, vérité terrain
   `evals/tickets.jsonl`, horloge simulée repartie du 2026-07-06 12:00, pannes Chaos arrêtées
   (leur table est recréée).
3. Vide les tables d'AgentPilot : exécutions, journal des appels d'outils, actions en attente ;
   interrupteurs à l'état du premier lancement (boucle en pause, pas d'arrêt d'urgence).

Les logs (`logs/`) et les décisions de la CLI headless (`runs/support/`) ne sont pas touchés.
ShopSim est joint par `DATABASE_URL`, AgentPilot par `AGENTPILOT_DATABASE_URL` : par défaut la
même base.
"""

import argparse
import sys
from collections.abc import Callable, Sequence

from sqlalchemy import Engine

from agentpilot.db import clear, engine, init_db, service_lock

CONFIRM = "Remettre ShopSim et AgentPilot à zéro ? Tout l'historique sera effacé. [o/N] "
YES = frozenset({"o", "oui", "y", "yes"})


def confirmed(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Sans terminal, ajouter --yes pour confirmer la remise à zéro.", file=sys.stderr)
        return False
    return input(CONFIRM).strip().lower() in YES


def seed_shopsim() -> None:
    from shopsim import seed

    seed.main()


def main(
    argv: Sequence[str] | None = None,
    bind: Engine = engine,
    seed: Callable[[], None] = seed_shopsim,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentpilot.reset",
        description="Relance le seed de ShopSim et vide les tables d'AgentPilot.",
    )
    parser.add_argument("--yes", action="store_true", help="ne demande pas de confirmation")
    args = parser.parse_args(argv)
    if not confirmed(args):
        print("Remise à zéro abandonnée.", file=sys.stderr)
        return 1
    init_db(bind)
    with service_lock(bind) as locked:
        if not locked:
            print(
                "Le service AgentPilot est lancé (ou une autre remise à zéro) : l'arrêter "
                "avant de remettre à zéro.",
                file=sys.stderr,
            )
            return 1
        seed()
        clear(bind)
    print(
        "AgentPilot : exécutions, journal et actions en attente vidés ; boucle en pause, "
        "arrêt d'urgence levé."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
