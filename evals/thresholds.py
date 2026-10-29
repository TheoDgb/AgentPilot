"""Seuils de réussite d'une évaluation (J5.4), versionnés dans `evals/thresholds.toml`.

Un score global minimum (tickets réussis sur tous leurs critères applicables) et des critères
stricts (aucun échec admis). Taux sur tous les passages ; un critère sans cas applicable est
réussi. Un passage arrêté avant la fin fait échouer la vérification : un score calculé sur des
tickets manquants ne doit jamais passer pour une réussite.

Utilisé par `python -m evals.grade --thresholds` (code de sortie 3 si un seuil n'est pas
atteint) et par le workflow d'évaluation de la CI.
"""

import hashlib
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.grading import Criterion, Score, TicketGrade, by_criterion, overall
from evals.results import EvalReport, EvalStatus

DEFAULT_PATH = Path(__file__).parent / "thresholds.toml"


class InvalidThresholds(ValueError):
    """Fichier de seuils illisible ou incohérent."""


@dataclass(frozen=True)
class Thresholds:
    min_rate: float
    strict: tuple[Criterion, ...]
    sha256: str


def load(path: Path = DEFAULT_PATH) -> Thresholds:
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        min_rate = data["overall"]["min_rate"]
        names = data["strict"]["criteria"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidThresholds(f"{path.name} illisible : {exc}") from None
    if not isinstance(min_rate, int | float) or isinstance(min_rate, bool):
        raise InvalidThresholds(f"{path.name} : overall.min_rate doit être un nombre")
    if not 0 <= min_rate <= 1:
        raise InvalidThresholds(f"{path.name} : overall.min_rate hors de [0, 1] ({min_rate})")
    known = {c.value for c in Criterion}
    if not isinstance(names, list) or any(n not in known for n in names):
        raise InvalidThresholds(
            f"{path.name} : strict.criteria doit être une liste parmi {', '.join(sorted(known))}"
        )
    return Thresholds(
        min_rate=float(min_rate),
        strict=tuple(Criterion(n) for n in names),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@dataclass(frozen=True)
class Outcome:
    """Un seuil : sa description, le score atteint et s'il est atteint."""

    name: str
    score: Score
    required: float
    met: bool


def _rate(score: Score) -> str:
    return "—" if score.rate is None else f"{score.rate * 100:.0f} %"


def describe(outcome: Outcome) -> str:
    return (
        f"{outcome.name} {outcome.score.passed} / {outcome.score.total} "
        f"({_rate(outcome.score)}), {outcome.required * 100:.0f} % exigé"
    )


def check(
    reports: Sequence[EvalReport], grades: Sequence[Sequence[TicketGrade]], thresholds: Thresholds
) -> tuple[list[Outcome], list[str]]:
    """(chaque seuil et son résultat, motifs d'échec) ; aucun motif : tous atteints."""
    every = [g for grades_of_pass in grades for g in grades_of_pass]
    total = overall(every)
    outcomes = [
        Outcome(
            name="score global",
            score=total,
            required=thresholds.min_rate,
            met=total.total > 0 and (total.rate or 0) >= thresholds.min_rate,
        )
    ]
    scores = by_criterion(every)
    for criterion in thresholds.strict:
        score = scores[criterion]
        outcomes.append(
            Outcome(
                name=criterion.value,
                score=score,
                required=1.0,
                # Aucun cas applicable : rien n'a échoué
                met=score.passed == score.total,
            )
        )
    failures = [f"Seuil non atteint : {describe(o)}" for o in outcomes if not o.met]
    incomplete = [
        r.summary.status.value for r in reports if r.summary.status is not EvalStatus.COMPLETED
    ]
    if incomplete:
        failures.insert(
            0,
            f"Évaluation incomplète ({', '.join(incomplete)}) : seuils non vérifiables sur des "
            "tickets manquants",
        )
    return outcomes, failures
