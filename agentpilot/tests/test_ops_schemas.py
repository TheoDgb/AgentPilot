"""Rapport d'incident de l'agent Ops (`agentpilot/ops_schemas.py`) : liste fermée des causes,
preuves citées, et chaque règle de cohérence."""

from typing import Any

import pytest
from pydantic import ValidationError

from agentpilot.ops_schemas import ROUTE_CAUSES, IncidentCause, IncidentReport

ROUTE = "/orders/{order_id}"


def evidence(tool: str = "summarize_logs", value: Any = 516) -> dict[str, Any]:  # noqa: ANN401
    return {
        "tool": tool,
        "arguments": {"service": "shopsim", "since_minutes": 5},
        "field": f"requests.routes[{ROUTE}].5xx",
        "value": value,
        "observed": "il y a 2 s",
    }


def hypothesis(cause: str, status: str, with_evidence: bool = True) -> dict[str, Any]:
    return {
        "cause": cause,
        "status": status,
        "reasoning": "Ce que montrent les preuves.",
        "evidence": [evidence()] if with_evidence else [],
    }


def report(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """Rapport valide : erreurs de base confirmées, saturation écartée."""
    return {
        "alert_id": "6b1f8d0e-1c2a-4f1a-9a55-0c6f0b0f8a11",
        "alertname": "ShopSimHighErrorRate",
        "alert_route": ROUTE,
        "summary": "Les lectures des commandes échouent sur une erreur de base de données.",
        "symptoms": [
            {
                "description": "100 % de 5xx sur les commandes",
                "routes": [ROUTE],
                "evidence": [evidence()],
            }
        ],
        "hypotheses": [
            hypothesis("database_errors", "confirmed"),
            hypothesis("resource_saturation", "ruled_out"),
        ],
        "probable_cause": "database_errors",
        "confidence": "high",
        "affected_routes": [ROUTE],
        "proposed_fix": "Vérifier l'état de la base et ses connexions.",
        "next_checks": [],
    } | changes


def refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        IncidentReport.model_validate(data)


def test_valid_report_and_cited_tools() -> None:
    parsed = IncidentReport.model_validate(report())

    assert parsed.probable_cause is IncidentCause.DATABASE_ERRORS
    assert parsed.cited_tools() == {"summarize_logs"}


def test_causes_are_a_closed_list_of_generic_families() -> None:
    assert {c.value for c in IncidentCause} == {
        "database_errors",
        "resource_saturation",
        "slow_dependency",
        "application_error",
        "service_unreachable",
        "agent_platform",
        "no_incident",
        "undetermined",
    }
    refused(report(probable_cause="db_errors"), "probable_cause")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        # Preuve tirée d'un outil qui n'est pas un des 5 outils Ops
        (
            report(
                symptoms=[{"description": "x", "routes": [], "evidence": [evidence("get_order")]}]
            ),
            "tool",
        ),
        # Symptôme sans preuve
        (report(symptoms=[{"description": "x", "routes": [], "evidence": []}]), "evidence"),
        # Une seule hypothèse
        (report(hypotheses=[hypothesis("database_errors", "confirmed")]), "hypotheses"),
        # Cause probable différente de l'hypothèse confirmée
        (report(probable_cause="slow_dependency"), "seule hypothèse confirmée"),
        # Deux hypothèses confirmées
        (
            report(
                hypotheses=[
                    hypothesis("database_errors", "confirmed"),
                    hypothesis("application_error", "confirmed"),
                    hypothesis("resource_saturation", "ruled_out"),
                ]
            ),
            "seule hypothèse confirmée",
        ),
        # Aucune hypothèse écartée
        (
            report(
                hypotheses=[
                    hypothesis("database_errors", "confirmed"),
                    hypothesis("resource_saturation", "inconclusive", with_evidence=False),
                ]
            ),
            "écartée",
        ),
        # Hypothèse écartée sans preuve
        (
            report(
                hypotheses=[
                    hypothesis("database_errors", "confirmed"),
                    hypothesis("resource_saturation", "ruled_out", with_evidence=False),
                ]
            ),
            "sans preuve",
        ),
        # Deux fois la même cause
        (
            report(
                hypotheses=[
                    hypothesis("database_errors", "confirmed"),
                    hypothesis("database_errors", "ruled_out"),
                ]
            ),
            "même cause",
        ),
        # Champ inconnu
        (report(severity="critical"), "extra"),
    ],
)
def test_inconsistent_reports_are_refused(data: dict[str, Any], message: str) -> None:
    refused(data, message)


def undetermined(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return (
        report(
            hypotheses=[
                hypothesis("database_errors", "inconclusive"),
                hypothesis("resource_saturation", "ruled_out"),
            ],
            probable_cause="undetermined",
            confidence="medium",
        )
        | changes
    )


def test_undetermined_cause_confirms_nothing_and_is_never_highly_confident() -> None:
    IncidentReport.model_validate(undetermined())
    refused(undetermined(confidence="high"), "ne peut pas être élevée")
    refused(
        undetermined(
            hypotheses=[
                hypothesis("database_errors", "confirmed"),
                hypothesis("resource_saturation", "ruled_out"),
            ]
        ),
        "aucune hypothèse ne peut être confirmée",
    )


def with_cause(cause: str, routes: list[str]) -> dict[str, Any]:
    other = "database_errors" if cause != "database_errors" else "slow_dependency"
    return report(
        hypotheses=[hypothesis(cause, "confirmed"), hypothesis(other, "ruled_out")],
        probable_cause=cause,
        affected_routes=routes,
    )


def test_no_incident_touches_no_route() -> None:
    IncidentReport.model_validate(with_cause("no_incident", []))
    refused(with_cause("no_incident", [ROUTE]), "aucune route touchée")


@pytest.mark.parametrize("cause", ["agent_platform", "service_unreachable"])
@pytest.mark.parametrize("routes", [[], [ROUTE]])
def test_platform_and_unreachable_may_touch_no_route(cause: str, routes: list[str]) -> None:
    """Ces pannes peuvent ne toucher aucune route de ShopSim (AgentPilot, Prometheus…)."""
    IncidentReport.model_validate(with_cause(cause, routes))


@pytest.mark.parametrize("cause", sorted(c.value for c in ROUTE_CAUSES))
def test_shop_failures_need_at_least_one_route(cause: str) -> None:
    IncidentReport.model_validate(with_cause(cause, [ROUTE]))
    refused(with_cause(cause, []), "au moins une route")


def test_route_causes_are_the_four_shop_failures() -> None:
    assert {c.value for c in ROUTE_CAUSES} == {
        "database_errors",
        "resource_saturation",
        "slow_dependency",
        "application_error",
    }


@pytest.mark.parametrize("routes", [[], [ROUTE]])
def test_undetermined_accepts_both(routes: list[str]) -> None:
    IncidentReport.model_validate(undetermined(affected_routes=routes))
