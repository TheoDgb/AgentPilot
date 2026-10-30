"""Comparaison de deux séries de l'agent Ops (J6.4) : agent seul contre subagents, par exemple.

    python -m evals.ops_compare --base evals/results/ops-<A>.json \
        --candidate evals/results/ops-<B>.json [--out <rapport>]

Les deux séries sont renotées ici à partir des résultats bruts, avec la notation et la vérité
terrain en place (`evals.ops_grade.load`) : même version par construction. Refus si les séries
ne portent pas sur les mêmes enquêtes, si elles ont la même architecture et la même consigne
(rien à comparer), ou si un passage s'est arrêté avant la fin.

**Règle de décision, fixée avant l'essai** (journal, J6.4) : la candidate est retenue seulement
si elle est sans régression **et** moins chère ou plus rapide que la base, sur les enquêtes
notées des deux côtés. Régression : une enquête réussie par la base et ratée par la candidate,
une faute grave, ou plus de preuves introuvables. Le verdict du rapport n'est que ce calcul ;
la décision reste humaine.

Les tours (`num_turns`) ne comptent que ceux de l'agent principal : avec des subagents, les
appels d'outils sont la mesure comparable. Pas de test de signe : avec un passage et une base
à 100 %, il ne dirait rien.

Rapport Markdown dans `docs/evals/ops-compare-<A>-vs-<B>.md` (versionné).
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from evals.incident_truth import INCIDENTS_PATH, truth_sha256
from evals.ops_evidence import EvidenceVerdict
from evals.ops_grade import REPORTS_DIR, Refused, load
from evals.ops_grading import ScenarioGrade, rules_sha256
from evals.ops_report import OpsPass
from evals.ops_results import RunStatus, ScenarioResult, unit_label
from evals.report import cell, money

Unit = tuple[str, str | None]
LEAD = "principal"
# Outils de délégation : un lancement de subagent n'est pas une lecture
DELEGATION = {"Agent", "Task"}


@dataclass
class Side:
    """Une enquête dans une série : ses notes et ses mesures sur les passages."""

    grades: list[ScenarioGrade] = field(default_factory=list)
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def graded(self) -> list[ScenarioGrade]:
        return [g for g in self.grades if g.graded]

    @property
    def pass_rate(self) -> float | None:
        graded = self.graded
        return None if not graded else sum(bool(g.passed) for g in graded) / len(graded)

    @property
    def grave(self) -> int:
        return sum(g.grave_fault for g in self.graded)

    @property
    def unfound(self) -> int:
        return sum(g.evidence_counts()[EvidenceVerdict.FABRICATED] for g in self.graded)

    def _mean(self, values: list[float | None]) -> float | None:
        known = [v for v in values if v is not None]
        return mean(known) if known else None

    @property
    def cost(self) -> float | None:
        return self._mean([r.cost_usd for r in self.results])

    @property
    def duration_s(self) -> float | None:
        return self._mean(
            [None if r.duration_ms is None else r.duration_ms / 1000 for r in self.results]
        )

    @property
    def turns(self) -> float | None:
        return self._mean(
            [None if r.num_turns is None else float(r.num_turns) for r in self.results]
        )

    @property
    def reads(self) -> float | None:
        """Appels aux outils de lecture exécutés (hors lancements de subagents)."""
        return self._mean([float(len(_reads(r))) for r in self.results if r.report is not None])

    def by_agent(self) -> Counter[str]:
        found: Counter[str] = Counter()
        for result in self.results:
            for call in _reads(result):
                found[call.get("agent") or LEAD] += 1
        return found


def _reads(result: ScenarioResult) -> list[dict[str, object]]:
    return [
        c
        for c in result.tool_calls
        if c.get("outcome") == "succeeded" and c.get("tool") not in DELEGATION
    ]


@dataclass
class Series:
    name: str
    architecture: str
    prompt: str
    passes: list[OpsPass]
    units: dict[Unit, Side]


def series(paths: Sequence[Path], role: str) -> Series:
    try:
        passes = load(paths, INCIDENTS_PATH)
    except Refused as exc:
        raise Refused(f"{role} : {exc}") from exc
    stopped = [p.path.name for p in passes if p.report.summary.status is not RunStatus.COMPLETED]
    if stopped:
        raise Refused(f"{role} : passage arrêté avant la fin ({', '.join(stopped)})")
    units: dict[Unit, Side] = {}
    for p in passes:
        for result, grade in zip(p.report.scenarios, p.grades, strict=True):
            side = units.setdefault((result.scenario_id, result.investigation), Side())
            side.grades.append(grade)
            side.results.append(result)
    summary = passes[0].report.summary
    return Series(summary.series, summary.architecture, summary.prompt_sha256, passes, units)


@dataclass(frozen=True)
class Change:
    unit: Unit
    base: str
    candidate: str
    detail: str


@dataclass
class Comparison:
    base: Series
    candidate: Series
    regressions: list[Change]
    improvements: list[Change]
    # Enquêtes non notées d'un côté au moins : hors des totaux
    incomplete: list[Unit]
    totals: dict[str, tuple[float, float]]
    verdict: str
    reason: str


def _verdict_text(side: Side) -> str:
    rate = side.pass_rate
    if rate is None:
        return "non notée"
    causes = sorted({g.found_cause or "aucun rapport" for g in side.graded})
    return f"{rate * 100:.0f} % ({', '.join(causes)})"


def _criteria_changes(base: Side, candidate: Side) -> str:
    def failing(side: Side) -> set[str]:
        return {
            c.criterion.value for g in side.graded for c in g.criteria if c.verdict.value == "fail"
        }

    gained, lost = failing(candidate) - failing(base), failing(base) - failing(candidate)
    parts = [f"en échec : {', '.join(sorted(gained))}"] if gained else []
    parts += [f"réparés : {', '.join(sorted(lost))}"] if lost else []
    return " ; ".join(parts)


def compare(base: Series, candidate: Series) -> Comparison:
    if set(base.units) != set(candidate.units):
        raise Refused("les deux séries ne portent pas sur les mêmes enquêtes")
    if (base.architecture, base.prompt) == (candidate.architecture, candidate.prompt):
        raise Refused("même architecture et même consigne : rien à comparer")
    regressions, improvements, incomplete = [], [], []
    totals = {key: [0.0, 0.0] for key in ("cost", "duration_s", "turns", "reads")}
    for unit in base.units:
        a, b = base.units[unit], candidate.units[unit]
        if a.pass_rate is None or b.pass_rate is None:
            incomplete.append(unit)
            continue
        for key in totals:
            totals[key][0] += getattr(a, key) or 0.0
            totals[key][1] += getattr(b, key) or 0.0
        reasons = []
        if b.pass_rate < a.pass_rate:
            reasons.append("taux de réussite en baisse")
        if b.grave > a.grave:
            reasons.append("faute grave")
        if b.unfound > a.unfound:
            reasons.append(f"preuves introuvables : {a.unfound} → {b.unfound}")
        change = Change(unit, _verdict_text(a), _verdict_text(b), _criteria_changes(a, b))
        if reasons:
            regressions.append(
                Change(unit, change.base, change.candidate, "; ".join(reasons + [change.detail]))
            )
        elif b.pass_rate > a.pass_rate:
            improvements.append(change)
    cost, duration = totals["cost"], totals["duration_s"]
    if incomplete:
        verdict, reason = "sans verdict", f"{len(incomplete)} enquête(s) non notée(s) d'un côté"
    elif regressions:
        verdict, reason = "pas retenue", f"{len(regressions)} régression(s)"
    elif cost[1] < cost[0] or duration[1] < duration[0]:
        gains = [n for n, (x, y) in (("moins chère", cost), ("plus rapide", duration)) if y < x]
        verdict, reason = "retenue", "sans régression, " + " et ".join(gains)
    else:
        verdict, reason = "pas retenue", "sans régression, mais ni moins chère ni plus rapide"
    return Comparison(
        base,
        candidate,
        regressions,
        improvements,
        incomplete,
        {k: (v[0], v[1]) for k, v in totals.items()},
        verdict,
        reason,
    )


# --- Rapport -------------------------------------------------------------------------------


def _delta(before: float, after: float) -> str:
    if before == 0:
        return "—"
    return f"{(after - before) / before * 100:+.0f} %".replace(".", ",")


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _score(s: Series) -> str:
    graded = [g for side in s.units.values() for g in side.graded]
    passed = sum(bool(g.passed) for g in graded)
    grave = sum(g.grave_fault for g in graded)
    return f"{passed} / {len(graded)} (fautes graves : {grave})"


def _evidence(s: Series) -> str:
    counts: Counter[EvidenceVerdict] = Counter()
    for side in s.units.values():
        for g in side.graded:
            counts.update(g.evidence_counts())
    return (
        f"{counts[EvidenceVerdict.EXACT]} / {counts[EvidenceVerdict.FOUND]} / "
        f"{counts[EvidenceVerdict.UNVERIFIABLE]} / {counts[EvidenceVerdict.FABRICATED]}"
    )


def _changes(title: str, changes: list[Change]) -> list[str]:
    if not changes:
        return [f"## {title}", "", "Aucune.", ""]
    return [
        f"## {title}",
        "",
        "| Enquête | Base | Candidate | Détail |",
        "|---|---|---|---|",
        *(
            f"| `{unit_label(*c.unit)}` | {cell(c.base)} | {cell(c.candidate)} | {cell(c.detail)} |"
            for c in changes
        ),
        "",
    ]


def render(result: Comparison, *, graded_at: datetime) -> str:
    a, b = result.base, result.candidate
    cost, duration = result.totals["cost"], result.totals["duration_s"]
    turns, reads = result.totals["turns"], result.totals["reads"]
    lines = [
        f"# Comparaison de l'agent Ops : {a.name} contre {b.name}",
        "",
        f"- Base : `{a.name}`, architecture `{a.architecture}`, consigne `{a.prompt[:12]}`, "
        f"{len(a.passes)} passage(s)",
        f"- Candidate : `{b.name}`, architecture `{b.architecture}`, consigne `{b.prompt[:12]}`, "
        f"{len(b.passes)} passage(s)",
        f"- Renotées le {graded_at:%Y-%m-%d %H:%M} UTC par `python -m evals.ops_compare`, "
        f"version de la notation `{rules_sha256()[:12]}`, vérité terrain "
        f"`{truth_sha256(INCIDENTS_PATH)[:12]}`",
        "",
        "## Verdict",
        "",
        f"**Candidate {result.verdict}** : {result.reason}.",
        "",
        "Règle fixée avant l'essai : retenue seulement sans régression (aucune enquête réussie "
        "par la base et ratée par la candidate, aucune faute grave ni preuve introuvable de "
        "plus) **et** moins chère ou plus rapide, sur les enquêtes notées des deux côtés. Ce "
        "verdict n'est qu'un calcul : la décision reste humaine.",
        "",
        "## Score et preuves",
        "",
        "| | Base | Candidate |",
        "|---|---|---|",
        f"| Enquêtes réussies | {_score(a)} | {_score(b)} |",
        f"| Preuves exactes / retrouvées / invérifiables / introuvables | {_evidence(a)} | "
        f"{_evidence(b)} |",
        "",
        "## Coût, durée, tours et appels",
        "",
        "Totaux sur les enquêtes notées des deux côtés (moyenne des passages par enquête). Les "
        "tours ne comptent que ceux de l'agent principal : avec des subagents, les appels aux "
        "outils de lecture sont la mesure comparable.",
        "",
        "| | Base | Candidate | Écart |",
        "|---|---|---|---|",
        f"| Coût | {money(cost[0])} | {money(cost[1])} | {_delta(*cost)} |",
        f"| Durée des enquêtes | {_num(duration[0], 0)} s | {_num(duration[1], 0)} s | "
        f"{_delta(*duration)} |",
        f"| Tours (agent principal) | {_num(turns[0], 0)} | {_num(turns[1], 0)} | "
        f"{_delta(*turns)} |",
        f"| Appels aux outils de lecture | {_num(reads[0], 0)} | {_num(reads[1], 0)} | "
        f"{_delta(*reads)} |",
        "",
        "| Enquête | Coût base | Coût candidate | Durée base | Durée candidate | Appels base | "
        "Appels candidate |",
        "|---|---|---|---|---|---|---|",
    ]
    for unit, side in a.units.items():
        other = b.units[unit]
        lines.append(
            f"| `{unit_label(*unit)}` | {money(side.cost)} | {money(other.cost)} | "
            f"{_num(side.duration_s, 0)} s | {_num(other.duration_s, 0)} s | "
            f"{_num(side.reads, 0)} | {_num(other.reads, 0)} |"
        )
    agents: Counter[str] = Counter()
    for side in b.units.values():
        agents.update(side.by_agent())
    by_agent = ", ".join(f"{name} {count}" for name, count in sorted(agents.items()))
    lines += ["", f"Appels de la candidate par agent (tous passages) : {by_agent or 'aucun'}."]
    lines += ["", *_changes("Régressions", result.regressions)]
    lines += _changes("Améliorations", result.improvements)
    if result.incomplete:
        lines += [
            "## Enquêtes non notées d'un côté",
            "",
            *(f"- `{unit_label(*u)}`" for u in result.incomplete),
            "",
        ]
    lines += [
        "## Limites",
        "",
        "- Pas de test de signe : avec un passage et une base déjà à 100 %, il ne dirait rien. "
        "Une différence d'une enquête peut venir du hasard.",
        "- Les durées dépendent aussi de l'API au moment de chaque série.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, reports_dir: Path = REPORTS_DIR) -> int:
    """0 : rapport écrit ; 1 : refusé."""
    parser = argparse.ArgumentParser(prog="python -m evals.ops_compare")
    parser.add_argument("--base", nargs="+", type=Path, required=True)
    parser.add_argument("--candidate", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare(series(args.base, "base"), series(args.candidate, "candidate"))
    except Refused as exc:
        print(f"Comparaison refusée : {exc}", file=sys.stderr)
        return 1
    out = args.out or reports_dir / f"ops-compare-{result.base.name}-vs-{result.candidate.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result, graded_at=datetime.now(UTC)), encoding="utf-8")
    print(f"Candidate {result.verdict} : {result.reason}")
    print(f"Régressions : {len(result.regressions)} ; améliorations : {len(result.improvements)}")
    print(f"Rapport : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
