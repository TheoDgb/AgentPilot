"""Vérité terrain des incidents (J6.3) : cohérence, et jamais à portée de l'agent Ops."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentpilot import ops_agent
from agentpilot.ops_schemas import IncidentCause
from evals.incident_truth import INCIDENTS_PATH, IncidentScenario, load_scenarios
from evals.ops_run import LOG_FILE
from observability.query import SERVICES
from shopsim.app.main import app
from shopsim.app.models import ChaosScenario

# Gabarits de route de ShopSim, tels que les voient les logs et les métriques
ROUTES = set(app.openapi()["paths"])
REPO = Path(__file__).resolve().parents[2]


def test_the_six_scenarios() -> None:
    scenarios = {s.scenario_id: s for s in load_scenarios()}

    assert set(scenarios) == {
        "db_errors",
        "payment_latency",
        "saturation",
        "bad_deploy",
        "saturation_then_payments",
        "false_alert",
    }
    assert scenarios["false_alert"].expected_cause is IncidentCause.NO_INCIDENT
    assert scenarios["false_alert"].inject_alert is not None
    assert scenarios["bad_deploy"].load is not None
    chain = scenarios["saturation_then_payments"]
    assert [s.fault for s in chain.steps] == [
        ChaosScenario.CONNECTION_SATURATION,
        ChaosScenario.PAYMENT_LATENCY,
    ]
    assert chain.alert.started_before_step == 1
    # Chaque panne seule est un scénario (bad_deploy avec sa charge)
    single = {s.steps[0].fault for s in scenarios.values() if len(s.steps) == 1}
    assert single == set(ChaosScenario)


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s.scenario_id)
def test_routes_are_shopsim_route_templates(scenario: IncidentScenario) -> None:
    for route in [*scenario.expected_routes, *scenario.allowed_routes, scenario.alert.route]:
        assert route is None or route in ROUTES, route


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"expected_cause": "db_errors"}, "expected_cause"),
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
        ({"expected_cause": "no_incident"}, "aucune route attendue"),
        ({"steps": [], "inject_alert": None}, "aucune alerte ne viendrait"),
        ({"load": {"per_minute": 12, "during_step": 3}}, "étape inexistante"),
        ({"alert": {"alertname": "X", "route": None, "started_before_step": 0}}, "première"),
        ({"allowed_routes": ["/orders/{order_id}"]}, "à la fois"),
        ({"secret": "x"}, "extra"),
    ],
)
def test_invalid_scenarios_are_refused(changes: dict[str, object], message: str) -> None:
    base = json.loads(INCIDENTS_PATH.read_text(encoding="utf-8").splitlines()[0])
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
