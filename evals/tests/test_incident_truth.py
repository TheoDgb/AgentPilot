"""Vérité terrain des incidents (J6.3) : cohérence, et jamais à portée de l'agent Ops."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentpilot import ops_agent
from agentpilot.ops_schemas import IncidentCause
from evals.incident_truth import (
    FAULT_ROUTES,
    INCIDENTS_PATH,
    IncidentScenario,
    load_scenarios,
    truth_sha256,
)
from evals.ops_run import LOG_FILE
from observability.query import SERVICES
from shopsim.app.main import app
from shopsim.app.models import ChaosScenario

# Gabarits de route de ShopSim, tels que les voient les logs et les métriques
ROUTES = set(app.openapi()["paths"])
REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = {s.scenario_id: s for s in load_scenarios()}

# Version figée de la vérité terrain des incidents : toute modification est une nouvelle
# version, déclarée ici et notée dans le journal. Versions :
# - a05ce017d47d : J6.3, 6 scénarios (gardée dans fixtures/incidents-a05ce017d47d.jsonl)
# - 1d0c1c3e153b : J6.4, 11 scénarios, plusieurs enquêtes par scénario
FROZEN_TRUTH_SHA256 = "1d0c1c3e153b7f4e92f9c26e732dcfb5b2020a9fc284ebd91cfd87129757aadc"
J63 = FIXTURES / "incidents-a05ce017d47d.jsonl"

# Panne derrière chaque cause attendue, pour vérifier les routes des scénarios à deux pannes
FAULTS_OF = {
    IncidentCause.DATABASE_ERRORS: {ChaosScenario.DB_ERRORS, ChaosScenario.DB_ERRORS_INTERMITTENT},
    IncidentCause.SLOW_DEPENDENCY: {ChaosScenario.PAYMENT_LATENCY},
    IncidentCause.APPLICATION_ERROR: {ChaosScenario.BAD_DEPLOY},
    IncidentCause.RESOURCE_SATURATION: {ChaosScenario.CONNECTION_SATURATION},
}


def test_the_eleven_scenarios() -> None:
    scenarios = SCENARIOS

    assert list(scenarios) == [
        # J6.3
        "db_errors",
        "payment_latency",
        "saturation",
        "bad_deploy",
        "saturation_then_payments",
        "false_alert",
        # J6.4 : plus difficiles
        "db_and_payments",
        "db_and_bad_deploy",
        "db_intermittent",
        "saturation_then_bad_deploy",
        "db_errors_ended",
    ]
    false_alert = scenarios["false_alert"]
    assert false_alert.investigations[0].expected_cause is IncidentCause.NO_INCIDENT
    assert false_alert.inject_alert is not None
    assert scenarios["bad_deploy"].load is not None
    chain = scenarios["saturation_then_payments"]
    assert [s.fault for s in chain.steps] == [
        ChaosScenario.CONNECTION_SATURATION,
        ChaosScenario.PAYMENT_LATENCY,
    ]
    assert chain.investigations[0].alert.started_before_step == 1
    # Chaque panne seule est un scénario (bad_deploy avec sa charge)
    single = {s.steps[0].fault for s in scenarios.values() if len(s.steps) == 1}
    assert single == set(ChaosScenario)
    # Deux enquêtes : une par panne
    both = scenarios["db_and_payments"].investigations
    assert [(i.name, i.expected_cause) for i in both] == [
        ("orders", IncidentCause.DATABASE_ERRORS),
        ("payments", IncidentCause.SLOW_DEPENDENCY),
    ]
    # Charge de remboursements pendant l'erreur applicative, jamais pendant une autre panne
    for scenario in scenarios.values():
        if scenario.load is not None:
            assert scenario.steps[scenario.load.during_step].fault is ChaosScenario.BAD_DEPLOY


def test_the_six_scenarios_of_j6_3_are_unchanged() -> None:
    """Même contenu que la version a05ce017d47d, seule la forme change (une liste d'enquêtes)."""
    single = ("alert", "investigate_after_min", "expected_cause", "expected_routes")
    for line in J63.read_text(encoding="utf-8").splitlines():
        old = json.loads(line)
        moved = {k: old.pop(k) for k in (*single, "allowed_routes") if k in old}

        assert (
            IncidentScenario.model_validate(old | {"investigations": [moved]})
            == (SCENARIOS[old["scenario_id"]])
        )


def test_ground_truth_version_is_frozen() -> None:
    assert truth_sha256() == FROZEN_TRUTH_SHA256, (
        "vérité terrain des incidents modifiée : nouvelle version à déclarer"
    )


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS.values() if len(s.steps) > 1], ids=lambda s: s.scenario_id
)
def test_routes_of_the_other_fault_are_never_expected_nor_allowed(
    scenario: IncidentScenario,
) -> None:
    """Garde-fou : avec deux pannes, les routes de l'autre panne sont interdites à l'enquête
    (ni attendues ni admises), et les routes attendues sont celles de sa propre panne."""
    for investigation in scenario.investigations:
        own = FAULTS_OF[investigation.expected_cause]
        own_routes = frozenset().union(*(FAULT_ROUTES[f] for f in own))
        others = scenario.fault_routes() - own_routes
        cited_ok = set(investigation.expected_routes) | set(investigation.allowed_routes)
        assert others, scenario.scenario_id
        assert not others & cited_ok, (scenario.scenario_id, investigation.name)
        assert set(investigation.expected_routes) <= own_routes


def test_every_fault_has_its_routes() -> None:
    assert set(FAULT_ROUTES) == set(ChaosScenario)
    for routes in FAULT_ROUTES.values():
        assert routes <= ROUTES


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s.scenario_id)
def test_routes_are_shopsim_route_templates(scenario: IncidentScenario) -> None:
    for i in scenario.investigations:
        for route in [*i.expected_routes, *i.allowed_routes, i.alert.route]:
            assert route is None or route in ROUTES, route


# Premier scénario (db_errors) et son enquête, pour les cas invalides
BASE = json.loads(INCIDENTS_PATH.read_text(encoding="utf-8").splitlines()[0])
INVESTIGATION = BASE["investigations"][0]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"investigations": [{**INVESTIGATION, "expected_cause": "db_errors"}]}, "expected_cause"),
        ({"steps": [{"fault": "cosmic_rays", "start_min": 0}]}, "fault"),
        (
            {
                "steps": [
                    {"fault": "db_errors", "start_min": 5},
                    {"fault": "bad_deploy", "start_min": 1},
                ]
            },
            "désordre",
        ),
        ({"steps": [{"fault": "db_errors", "start_min": 5, "stop_min": 2}]}, "fin avant"),
        (
            {"investigations": [{**INVESTIGATION, "expected_cause": "no_incident"}]},
            "aucune route attendue",
        ),
        ({"steps": [], "inject_alert": None}, "aucune alerte ne viendrait"),
        ({"load": {"per_minute": 12, "during_step": 3}}, "étape inexistante"),
        (
            {
                "investigations": [
                    {
                        **INVESTIGATION,
                        "alert": {"alertname": "X", "route": None, "started_before_step": 0},
                    }
                ]
            },
            "première",
        ),
        (
            {"investigations": [{**INVESTIGATION, "allowed_routes": ["/orders/{order_id}"]}]},
            "à la fois",
        ),
        ({"investigations": []}, "at least 1"),
        # Deux enquêtes : chacune nommée, noms différents, alertes différentes
        ({"investigations": [INVESTIGATION, {**INVESTIGATION, "name": "b"}]}, "chacune a un nom"),
        (
            {
                "investigations": [
                    {**INVESTIGATION, "name": "a"},
                    {**INVESTIGATION, "name": "a", "alert": {"alertname": "Y", "route": None}},
                ]
            },
            "tous différents",
        ),
        (
            {"investigations": [{**INVESTIGATION, "name": "a"}, {**INVESTIGATION, "name": "b"}]},
            "visée deux fois",
        ),
        ({"secret": "x"}, "extra"),
    ],
)
def test_invalid_scenarios_are_refused(changes: dict[str, object], message: str) -> None:
    base = BASE
    with pytest.raises(ValidationError, match=re.escape(message)):
        IncidentScenario.model_validate(base | changes)


def test_duplicate_scenario_ids_are_refused(tmp_path: Path) -> None:
    line = INCIDENTS_PATH.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "incidents.jsonl"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="même nom"):
        load_scenarios(path)


# --- Jamais à portée de l'agent Ops -----------------------------------------------------------


def agent_sources() -> list[Path]:
    """Code qui tourne pour l'agent Ops ou que ses outils exécutent, et sa configuration."""
    return [
        *sorted((REPO / "mcp_server" / "ops").glob("*.py")),
        REPO / "agentpilot" / "ops_agent.py",
        REPO / "agentpilot" / "ops_schemas.py",
        *sorted((REPO / "agents" / "ops").rglob("*.*")),
        # J6.4 : consignes de l'enquêteur principal et des subagents
        *sorted((REPO / "agents" / "ops-subagents").rglob("*.*")),
    ]


@pytest.mark.parametrize("path", agent_sources(), ids=lambda p: p.name)
def test_agent_code_never_reaches_the_ground_truth(path: Path) -> None:
    """Ni le fichier ni le module de la vérité terrain (la question piège
    `incidents_ground_truth_path` en parle sans le nommer)."""
    text = path.read_text(encoding="utf-8")
    assert "incidents.jsonl" not in text and "incident_truth" not in text
    assert not re.search(r"^\s*(from|import)\s+evals\b", text, re.MULTILINE)


def test_ops_mcp_server_environment_has_no_path_to_evals(tmp_path: Path) -> None:
    config = ops_agent.mcp_server_config({}, trace_id=None, log_dir=tmp_path)

    assert "evals" not in json.dumps(config)


def test_launcher_journal_is_never_a_log_the_agent_reads() -> None:
    assert Path(LOG_FILE).stem not in SERVICES
