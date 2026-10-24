"""Date de référence de la simulation.

ShopSim ne lit jamais l'horloge système : « maintenant » est fixe, pour que le
seed et les évaluations restent déterministes.
"""

from datetime import UTC, date, datetime

SIMULATION_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def simulation_today() -> date:
    return SIMULATION_NOW.date()
