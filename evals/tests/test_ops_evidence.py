"""Recoupement des preuves de l'agent Ops avec les vraies réponses d'outils (`evals.ops_evidence`).

Fixture : enquête réelle de J6.2 sur `payment_latency` (rapport et journal), dont les 11
preuves avaient été recoupées à la main.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evals.ops_evidence import EvidenceVerdict, check, is_trivial, report_evidence, resolve

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ops_run_payment_latency.json").read_text(
        encoding="utf-8"
    )
)
CALLS: list[dict[str, Any]] = FIXTURE["tool_calls"]
PAYMENTS = "/payments/{order_id}"


def evidence(tool: str, field: str, value: Any, **arguments: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"tool": tool, "field": field, "value": value, "arguments": arguments}


def verdict(item: dict[str, Any], calls: list[dict[str, Any]] | None = None) -> EvidenceVerdict:
    return check(item, CALLS if calls is None else calls).verdict


def test_every_evidence_of_the_real_investigation_is_exact() -> None:
    found = [check(e, CALLS) for e in report_evidence(FIXTURE["output"])]

    assert len(found) == 11
    assert {f.verdict for f in found} == {EvidenceVerdict.EXACT}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # `[clé]` sur un champ d'identité : route, labels.route, service, event
        # Sur 15 min, la médiane des paiements mêle avant et pendant la panne
        ("requests.routes[/payments/{order_id}].p50_ms", 110),
        ("series[/payments/{order_id}].p95_s.now", 4.875),
        ("services[ShopSim].status", "ok"),
        ("warnings[http.request].count", 39),
        # Préfixe `requests.` omis, index numérique
        ("routes[/clock].p95_ms", 6),
    ],
)
def test_lenient_paths(field: str, value: Any) -> None:  # noqa: ANN401
    tool = (
        "check_health"
        if field.startswith("services")
        else ("query_metrics" if field.startswith("series") else "summarize_logs")
    )
    arguments = (
        {"metric": "shopsim_latency_trend"}
        if tool == "query_metrics"
        else {"service": "shopsim", "since_minutes": 15}
        if tool == "summarize_logs"
        else {}
    )
    assert verdict(evidence(tool, field, value, **arguments)) is EvidenceVerdict.EXACT


def test_rounded_value_by_the_agent_is_exact_but_not_a_wrong_one() -> None:
    """4,875 arrondi à 4,88 ou 4,9 : même valeur ; 5,2 : fausse."""
    trend = {"metric": "shopsim_latency_trend"}
    field = f"series[{PAYMENTS}].p95_s.now"
    for rounded in (4.88, 4.9):
        assert verdict(evidence("query_metrics", field, rounded, **trend)) is EvidenceVerdict.EXACT
    assert verdict(evidence("query_metrics", field, 5.2, **trend)) is EvidenceVerdict.FABRICATED


def test_omitted_and_default_arguments() -> None:
    """`limit` omis par la preuve ; `window` absent de l'appel vaut sa valeur par défaut."""
    assert (
        verdict(
            evidence(
                "search_logs",
                "lines[0].duration_ms",
                4012,
                service="shopsim",
                route=PAYMENTS,
                since_minutes=5,
            )
        )
        is EvidenceVerdict.EXACT
    )
    assert (
        verdict(
            evidence(
                "query_metrics",
                f"series[{PAYMENTS}].p95_s.now",
                4.875,
                metric="shopsim_latency_trend",
                window="5m",
            )
        )
        is EvidenceVerdict.EXACT
    )


# --- Négatifs : une mauvaise preuve n'est jamais validée ------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        # Valeur modifiée
        evidence(
            "summarize_logs",
            f"requests.routes[{PAYMENTS}].p50_ms",
            4999,
            service="shopsim",
            since_minutes=3,
        ),
        # Mauvais outil (la valeur vient de summarize_logs)
        evidence(
            "search_logs",
            f"requests.routes[{PAYMENTS}].p50_ms",
            4011,
            service="shopsim",
            since_minutes=3,
        ),
        # Arguments différents de tout appel
        evidence(
            "summarize_logs",
            f"requests.routes[{PAYMENTS}].p50_ms",
            4011,
            service="shopsim",
            since_minutes=60,
        ),
        evidence(
            "summarize_logs",
            f"requests.routes[{PAYMENTS}].p50_ms",
            4011,
            service="mcp",
            since_minutes=3,
        ),
        # Route inexistante, valeur absente de la réponse
        evidence(
            "summarize_logs",
            "requests.routes[/refunds].p50_ms",
            4321,
            service="shopsim",
            since_minutes=3,
        ),
        # Outil jamais appelé
        evidence("list_alerts", "alerts[0].alertname", "ShopSimHighLatency"),
        # Chemin résolu sur une autre valeur (texte)
        evidence("check_health", "services[ShopSim].status", "down"),
    ],
)
def test_wrong_evidence_is_fabricated(item: dict[str, Any]) -> None:
    assert verdict(item) is EvidenceVerdict.FABRICATED


def test_failed_calls_never_back_evidence() -> None:
    calls = copy.deepcopy(CALLS)
    for call in calls:
        call["outcome"] = "failed"
    item = evidence("check_health", "services[ShopSim].status", "ok")

    assert verdict(item, calls) is EvidenceVerdict.FABRICATED


def test_value_found_elsewhere_when_the_path_does_not_resolve() -> None:
    """Chemin inventé, valeur non banale présente dans la réponse : `found`, pas `exact`."""
    item = evidence(
        "summarize_logs", "requests.latency.slowest", 4012, service="shopsim", since_minutes=15
    )

    assert verdict(item) is EvidenceVerdict.FOUND


# --- Valeurs banales : seul un chemin résolu compte ----------------------------------------


@pytest.mark.parametrize("value", [0, 1, True, False, None, ""])
def test_trivial_value_without_a_resolved_path_is_unverifiable(value: Any) -> None:  # noqa: ANN401
    """Même présente ailleurs dans la réponse (0 et 1 y sont partout), jamais `found`."""
    item = evidence(
        "summarize_logs", "requests.nowhere.value", value, service="shopsim", since_minutes=15
    )

    assert verdict(item) is EvidenceVerdict.UNVERIFIABLE


def test_trivial_value_on_a_resolved_path_is_compared() -> None:
    errors = {"metric": "shopsim_error_rate", "window": "5m"}
    assert verdict(evidence("query_metrics", f"series[{PAYMENTS}].value", 0, **errors)) is (
        EvidenceVerdict.EXACT
    )
    # Chemin résolu, valeur différente : la banalité n'excuse rien
    assert verdict(
        evidence(
            "summarize_logs",
            f"requests.routes[{PAYMENTS}].p50_ms",
            0,
            service="shopsim",
            since_minutes=3,
        )
    ) is (EvidenceVerdict.FABRICATED)
    assert (
        verdict(evidence("check_health", "services[AgentPilot].loop.emergency_stop", True))
        is EvidenceVerdict.FABRICATED
    )


def test_true_is_never_one() -> None:
    assert is_trivial(True) and is_trivial(0) and is_trivial("") and is_trivial(None)
    assert not is_trivial(4011) and not is_trivial("ok")
    health = next(c for c in CALLS if c["tool"].endswith("check_health"))
    assert resolve(health["response"], "services[AgentPilot].loop.loop_paused") == [True]
    assert verdict(evidence("check_health", "services[AgentPilot].loop.loop_paused", 1)) is (
        EvidenceVerdict.FABRICATED
    )
