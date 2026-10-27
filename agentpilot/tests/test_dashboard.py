"""Tableau de bord Grafana et configuration de Prometheus, fichiers du dépôt.

Chaque requête du tableau de bord est confrontée à ce que les services publient vraiment : nom
de métrique, étiquettes filtrées ou regroupées (`{status="pending"}`, `by (route)`), valeurs
attendues, et job de la cible. Une faute de frappe dans un filtre donnerait un panneau vide ou
à zéro sans aucune erreur dans Grafana : c'est ici qu'elle doit échouer.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from agentpilot import metrics
from agentpilot.api_schemas import ControlStatus
from agentpilot.support_agent import REPO_ROOT
from agentpilot.worker import Skip
from observability.metrics import HTTP_REGISTRY

MONITORING = REPO_ROOT / "monitoring"
DASHBOARD = MONITORING / "grafana" / "dashboards" / "agentpilot.json"
DATASOURCE = MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
PROMETHEUS = MONITORING / "prometheus" / "prometheus.yml"

# Sélecteur PromQL : nom de métrique, puis éventuellement `{étiquette="valeur", …}`
SELECTOR = re.compile(r"\b((?:http|agentpilot)_[a-z_]+)\b(?:\{([^}]*)\})?")
MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"')
GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
# Étiquettes ajoutées par Prometheus à chaque série de la cible
TARGET_LABELS = {"job", "instance"}
JOBS = {"http_": "shopsim", "agentpilot_": "agentpilot"}


@dataclass(frozen=True)
class Metric:
    labels: frozenset[str]
    # Valeurs possibles par étiquette, quand elles forment une énumération fermée
    values: dict[str, frozenset[str]]


def status() -> ControlStatus:
    """État complet, file comprise : toutes les jauges sont émises."""
    return ControlStatus(
        loop_paused=False,
        pause_reason=None,
        paused_by=None,
        paused_at=None,
        emergency_stop=False,
        emergency_by=None,
        emergency_at=None,
        spent_today_usd=Decimal("0.5"),
        daily_budget_usd=Decimal("5"),
        running_tickets=[],
        max_concurrent_runs=2,
        waiting_until=None,
        waiting_reason=None,
        abandoned_tickets=[],
        queue_checked_at=datetime.now(UTC),
        open_tickets=3,
        eligible_tickets=1,
        skipped_tickets={reason.value: 1 for reason in Skip},
    )


def registries() -> list[CollectorRegistry]:
    state = metrics.state_registry(status(), {}, datetime.now(UTC))
    return [HTTP_REGISTRY, metrics.EVENTS, state]


def exposed() -> dict[str, Metric]:
    """Séries publiées (noms tels que PromQL les voit : `_total`, `_bucket`…), leurs étiquettes,
    et les valeurs d'étiquettes connues d'avance (séries créées à zéro, énumérations)."""
    found: dict[str, Metric] = {}
    for registry in registries():
        samples: dict[str, dict[str, set[str]]] = {}
        for family in registry.collect():
            for sample in family.samples:
                for label, value in sample.labels.items():
                    samples.setdefault(sample.name, {}).setdefault(label, set()).add(value)
        for name, collector in registry._names_to_collectors.items():  # noqa: SLF001
            labels = set(getattr(collector, "_labelnames", ()))
            if name.endswith("_bucket"):
                labels.add("le")
            values = {k: frozenset(v) for k, v in samples.get(name, {}).items() if k != "le"}
            found[name] = Metric(frozenset(labels), values)
    # Outils : toutes les séries n'existent qu'une fois appelées
    for name in ("agentpilot_tool_calls_total",):
        values = dict(found[name].values)
        values["tool"] = frozenset(metrics.TOOLS | {metrics.OTHER_TOOL})
        found[name] = Metric(found[name].labels, values)
    return found


def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def panels(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [panel for panel in board["panels"] if panel["type"] != "row"]


def queries(board: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for panel in panels(board):
        for target in panel["targets"]:
            yield panel["title"], target["expr"]


def problems(expr: str, known: dict[str, Metric]) -> list[str]:
    """Ce qui, dans une requête, ne correspond à aucune série publiée."""
    found: list[str] = []
    selected = [(name, matchers or "") for name, matchers in SELECTOR.findall(expr)]
    for name, matchers in selected:
        metric = known.get(name)
        if metric is None:
            found.append(f"métrique inconnue : {name}")
            continue
        for label, operator, value in MATCHER.findall(matchers):
            if label == "job":
                expected = next(job for prefix, job in JOBS.items() if name.startswith(prefix))
                if operator != "=" or value != expected:
                    found.append(f'{name} : job="{value}" au lieu de job="{expected}"')
                continue
            if label not in metric.labels | TARGET_LABELS:
                found.append(f"{name} : étiquette inconnue {label}")
                continue
            allowed = metric.values.get(label)
            if allowed is None:
                continue
            if operator == "=" and value not in allowed:
                found.append(f'{name} : {label}="{value}" ne vaut jamais ({sorted(allowed)})')
            if operator == "=~" and not any(re.fullmatch(value, v) for v in allowed):
                found.append(f'{name} : {label}=~"{value}" ne trouve rien ({sorted(allowed)})')
    # Regroupement : chaque étiquette doit exister sur les métriques de la requête
    names = [name for name, _ in selected if name in known]
    for grouping in GROUPING.findall(expr):
        for label in (part.strip() for part in grouping.split(",") if part.strip()):
            if names and not any(label in known[n].labels | TARGET_LABELS for n in names):
                found.append(f"regroupement par {label}, absente de {sorted(set(names))}")
    return found


def test_dashboard_queries_match_the_exposed_series() -> None:
    known = exposed()

    found = {
        f"{title} : {problem}"
        for title, expr in queries(dashboard())
        for problem in problems(expr, known)
    }

    assert found == set()


def test_every_metric_family_has_a_panel() -> None:
    known = exposed()
    used = {name for _, expr in queries(dashboard()) for name, _ in SELECTOR.findall(expr)}

    families = {re.sub(r"_(total|bucket|count|sum)$", "", name) for name in known}
    families = {f for f in families if not f.endswith("_created")} - {"agentpilot_state_up"}
    covered = {re.sub(r"_(total|bucket|count|sum)$", "", name) for name in used}
    assert families - covered == set()


@pytest.mark.parametrize(
    ("expr", "problem"),
    [
        ('sum(agentpilot_pending_actions{status="waiting"})', 'status="waiting" ne vaut jamais'),
        ('sum(agentpilot_pending_actions{state="pending"})', "étiquette inconnue state"),
        ('agentpilot_loop_state{state="paused"}', 'state="paused" ne vaut jamais'),
        ('sum(agentpilot_tool_calls_total{tool="refund"})', 'tool="refund" ne vaut jamais'),
        ('sum(agentpilot_runs_finished_total{status=~"error.*"})', "ne trouve rien"),
        ('rate(http_requests_total{job="agentpilot"}[1m])', 'au lieu de job="shopsim"'),
        ("sum by (ticket_id) (agentpilot_pending_actions)", "regroupement par ticket_id"),
        ('sum(agentpilot_pending_action{status="pending"})', "métrique inconnue"),
    ],
)
def test_wrong_filters_are_caught(expr: str, problem: str) -> None:
    """Le contrôle lui-même : chaque faute typique est signalée."""
    found = problems(expr, exposed())

    assert any(problem in p for p in found), found


def test_correct_filters_pass() -> None:
    known = exposed()

    assert problems('sum(agentpilot_pending_actions{status="pending"})', known) == []
    assert problems('agentpilot_loop_state{state="paused_budget"}', known) == []
    assert problems('rate(http_requests_total{job="shopsim",status=~"5.."}[1m])', known) == []


def test_jobs_match_the_prometheus_configuration() -> None:
    config = PROMETHEUS.read_text(encoding="utf-8")

    assert set(re.findall(r"job_name:\s*(\S+)", config)) == set(JOBS.values())


def test_dashboard_uses_the_provisioned_datasource() -> None:
    board = dashboard()
    uid = re.search(r"^\s*uid:\s*(\S+)", DATASOURCE.read_text(encoding="utf-8"), re.MULTILINE)

    assert uid is not None
    for panel in panels(board):
        assert panel["datasource"]["uid"] == uid.group(1)
        assert {target["datasource"]["uid"] for target in panel["targets"]} == {uid.group(1)}
    ids = [panel["id"] for panel in board["panels"]]
    assert len(ids) == len(set(ids))
