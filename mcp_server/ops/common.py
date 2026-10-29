"""Heure réelle et durées déjà calculées, réponses compactes.

Les logs, les métriques et les alertes sont datés à l'heure réelle, pas à celle de la
simulation : chaque réponse donne `now`, et chaque élément daté son âge (`ago`, `age_s`), pour
que l'agent n'ait aucun calcul de date à faire.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Valeurs de texte coupées au-delà (messages d'erreur, chemins)
MAX_TEXT = 200


def _real_now() -> datetime:
    return datetime.now(UTC)


# Remplaçable par les tests (horloge figée)
clock: Callable[[], datetime] = _real_now


def now() -> datetime:
    return clock()


def stamp(moment: datetime) -> str:
    """Heure UTC à la seconde, lisible : 2026-10-29T17:10:57Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def age(moment: datetime, reference: datetime) -> dict[str, Any]:
    """`{"ago": "il y a 3 min 12 s", "age_s": 192}` ; une heure future (horloges décalées)
    compte pour zéro."""
    seconds = max(0, round((reference - moment).total_seconds()))
    return {"ago": ago_text(seconds), "age_s": seconds}


def ago_text(seconds: int) -> str:
    if seconds < 1:
        return "à l'instant"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"il y a {days} j {hours} h"
    if hours:
        return f"il y a {hours} h {minutes:02d} min"
    if minutes:
        return f"il y a {minutes} min {secs:02d} s"
    return f"il y a {secs} s"


def duration_text(seconds: int) -> str:
    """Durée seule (« 4 min 05 s »), sans « il y a »."""
    return ago_text(seconds).removeprefix("il y a ")


def short(value: Any) -> Any:  # noqa: ANN401
    """Texte coupé à `MAX_TEXT` caractères ; les autres valeurs inchangées."""
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[: MAX_TEXT - 1] + "…"
    return value


def rounded(value: float | None, digits: int = 4) -> float | None:
    """Arrondi lisible ; NaN ou infini (p95 sans trafic) → None, jamais 0."""
    if value is None or value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, digits)
