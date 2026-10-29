"""Faits cités confrontés aux réponses d'outils du journal (`evals/grounding.py`)."""

import json

from agentpilot.schemas import UsedFact
from evals.grounding import Check, check, cited, elapsed_days, same, values
from evals.results import ActionCall, ReadCall
from shopsim.app.clock import SIMULATION_NOW

NOW = SIMULATION_NOW


def read(tool: str, response: object, outcome: str = "succeeded") -> ReadCall:
    return ReadCall(tool=tool, params={}, outcome=outcome, response=response)


def fact(tool: str, field: str, value: str | int | bool | None) -> UsedFact:
    return UsedFact(tool=tool, field=field, value=value)


def test_values_found_at_any_depth_and_in_json_text() -> None:
    nested = {"orders": [{"id": 1, "status": "paid"}, {"id": 2, "status": "cancelled"}]}
    assert list(values(nested, "status")) == ["paid", "cancelled"]
    # Réponse MCP en blocs de texte JSON
    blocks = [{"type": "text", "text": json.dumps({"carrier": "Colifast"})}]
    assert list(values(blocks, "carrier")) == ["Colifast"]
    assert list(values("pas du JSON", "carrier")) == []


def test_values_compared_without_json_type_or_case() -> None:
    assert same(21, "21") and same("21", 21.0) and same("Colifast", "colifast")
    assert same(True, "true") and not same(True, 1)
    assert same("2026-06-15", "2026-06-15T10:00:00Z")
    assert not same("2026-06-15", "2026-06-16T10:00:00Z") and not same(21, 22)


def test_check_states() -> None:
    reads = [read("get_shipment", {"shipment": {"days_since_delivery": 21}})]
    assert check(fact("mcp__shopsim__get_shipment", "days_since_delivery", 21), reads) is (
        Check.VERIFIED
    )
    assert check(fact("get_shipment", "shipment.days_since_delivery", 21), reads) is (
        Check.VERIFIED
    )
    assert check(fact("get_shipment", "days_since_delivery", 22), reads) is Check.MISMATCH
    assert check(fact("get_payment", "amount_cents", 100), reads) is Check.NOT_READ
    failed = [read("get_payment", None, outcome="failed")]
    assert check(fact("get_payment", "amount_cents", 100), failed) is Check.NOT_READ
    clipped = [read("get_order", {"truncated": '{"orders": [{"id": 1'})]
    assert check(fact("get_order", "total_cents", 100), clipped) is Check.UNVERIFIABLE


def action(tool: str, outcome: str) -> ActionCall:
    return ActionCall(tool=tool, params={}, outcome=outcome)


def test_cited_response_of_an_executed_action_is_unverifiable() -> None:
    """Vrai cas (« baseline » p2, tickets 17 et 18) : `refund_order.status` cité après le
    remboursement ; seule la réponse d'une lecture est gardée."""
    done = fact("refund_order", "status", "done")
    assert check(done, [], [action("refund_order", "succeeded")]) is Check.UNVERIFIABLE
    # Négatifs : action en attente, en échec, jamais appelée, ou une autre action exécutée
    assert check(done, [], [action("refund_order", "held")]) is Check.NOT_READ
    assert check(done, [], [action("refund_order", "failed")]) is Check.NOT_READ
    assert check(done, [], []) is Check.NOT_READ
    assert check(done, [], [action("resend_order", "succeeded")]) is Check.NOT_READ


def test_error_of_a_failed_read_is_unverifiable() -> None:
    """Vrai cas (« baseline », ticket 30) : `get_shipment` sans livraison (pas encore expédiée) ;
    le message d'erreur n'est pas recopié dans les résultats."""
    failed = [read("get_shipment", None, outcome="failed")]
    error = fact("get_shipment", "error", "Aucune livraison pour la commande 199")
    assert check(error, failed) is Check.UNVERIFIABLE
    # Négatifs : autre champ d'une lecture en échec, outil jamais lu, lecture réussie sans erreur
    assert check(fact("get_shipment", "status", "delivered"), failed) is Check.NOT_READ
    assert check(error, []) is Check.NOT_READ
    assert check(error, [read("get_shipment", {"status": "delivered"})]) is Check.MISMATCH


def test_days_since_delivery_counted_like_the_tool() -> None:
    """Jours entamés, comme `days_since_delivery` (vrai cas : ticket 17, livré le 20 juin à
    9 h, 17 jours au 6 juillet à midi, alors que le calendrier en compte 16)."""
    morning = [read("get_shipment", {"delivered_at": "2026-06-20T09:00:00Z"})]
    assert elapsed_days("2026-06-20", NOW, morning) == {17}
    evening = [read("get_shipment", {"delivered_at": "2026-05-22T21:35:00Z"})]
    assert elapsed_days("2026-05-22", NOW, evening) == {45}
    # Heure inconnue (pas de lecture de ce jour-là) : toute heure du jour, deux valeurs au plus
    assert elapsed_days("2026-06-20", NOW, None) == {16, 17}
    other_day = [read("get_shipment", {"delivered_at": "2026-06-19T09:00:00Z"})]
    assert elapsed_days("2026-06-20", NOW, other_day) == {16, 17}


def test_equivalents_of_a_justification_fact() -> None:
    since = [fact("get_shipment", "days_since_delivery", 21)]
    assert cited("delivered_at", "2026-06-15", since, NOW)
    assert not cited("delivered_at", "2026-06-14", since, NOW)
    stamp = [fact("get_shipment", "delivered_at", "2026-06-15T09:30:00Z")]
    assert cited("delivered_at", "2026-06-15", stamp, NOW)
    payment = [fact("get_payment", "remaining_refundable_cents", 38509)]
    assert cited("refund_amount_cents", 38509, payment, NOW)
    # Sans équivalent d'outil : seulement dans les textes
    assert not cited("claimed_cents", 9600, [fact("get_ticket", "claimed_cents", 9600)], NOW)


def test_days_since_delivery_exact_when_the_delivery_time_was_read() -> None:
    morning = [read("get_shipment", {"delivered_at": "2026-06-20T09:00:00Z"})]
    assert cited(
        "delivered_at",
        "2026-06-20",
        [fact("get_shipment", "days_since_delivery", 17)],
        NOW,
        morning,
    )
    # Négatifs : compte du calendrier quand l'heure est connue, un jour de trop, de moins
    for wrong in (16, 18, 15):
        used = [fact("get_shipment", "days_since_delivery", wrong)]
        assert not cited("delivered_at", "2026-06-20", used, NOW, morning)
