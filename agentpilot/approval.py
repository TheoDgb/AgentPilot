"""Validation humaine des actions de l'agent Support : décidée par le code, jamais par l'agent.

Avant chaque action, le hook (`agentpilot/hooks.py`) relit lui-même ShopSim (`gather_facts`)
puis applique `evaluate`, une fonction pure. Ce que l'agent écrit (décision, motif, montant
annoncé) n'entre jamais dans le verdict, sauf le montant demandé, qui ne peut que relever la
valeur comparée au seuil : l'agent ne peut pas passer sous le seuil en sous-déclarant.

Les seuils et la règle « client à risque » viennent de `evals/policy.py`, sans copie.

Ces lectures sont celles de la plateforme, pas de l'agent : l'agent, lui, n'accède à ShopSim
que par le serveur MCP. On réutilise le client HTTP du serveur MCP (mêmes erreurs lisibles).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from evals.policy import APPROVAL_THRESHOLD_CENTS, RISK_WINDOW, is_risky_customer
from mcp_server import idempotency
from mcp_server.computed import parse_datetime, refunds_in_risk_window
from mcp_server.shopsim_client import get_json

# Outils d'action du serveur MCP, par nom court
REPLY = "reply_to_ticket"
REFUND = "refund_order"
RESEND = "resend_order"
ESCALATE = "escalate_ticket"
RESOLVE = "resolve_ticket"
# Actions qui peuvent exiger une validation humaine (escalader, c'est déjà confier à un humain)
GATED = frozenset({REFUND, RESEND})
ACTIONS = frozenset({REPLY, REFUND, RESEND, ESCALATE, RESOLVE})


class Reason(StrEnum):
    OVER_THRESHOLD = "over_threshold"
    RISKY_CUSTOMER = "risky_customer"


REASON_LABELS = {
    Reason.OVER_THRESHOLD: "montant supérieur au seuil de validation",
    Reason.RISKY_CUSTOMER: "client à risque",
}


class Kind(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    reasons: tuple[Reason, ...] = ()
    # Message à l'agent en cas de refus
    message: str = ""
    # Valeur comparée au seuil (remboursement ou renvoi)
    value_cents: int | None = None


ALLOW = Verdict(Kind.ALLOW)


def deny(message: str) -> Verdict:
    return Verdict(Kind.DENY, message=message)


@dataclass(frozen=True)
class OrderFacts:
    """Ce que le hook a lu dans ShopSim avant un remboursement ou un renvoi."""

    ticket_customer_id: int
    order_customer_id: int
    order_total_cents: int
    # Remboursement seulement : amount_cents − refunded_cents du paiement
    remaining_refundable_cents: int | None
    refund_dates: tuple[datetime, ...] = field(default=())
    now: datetime | None = None

    def record(self) -> dict[str, Any]:
        """Faits enregistrés avec l'action en attente."""
        assert self.now is not None
        return {
            "order_total_cents": self.order_total_cents,
            "remaining_refundable_cents": self.remaining_refundable_cents,
            "refunds_in_risk_window": refunds_in_risk_window(self.refund_dates, self.now),
            "risk_window_days": RISK_WINDOW.days,
            "as_of": self.now.isoformat(),
        }


def short_name(tool_name: str) -> str:
    """`mcp__shopsim__refund_order` → `refund_order`."""
    return tool_name.rsplit("__", 1)[-1]


def _int(value: Any) -> int | None:  # noqa: ANN401
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def check_ticket(tool: str, tool_input: Mapping[str, Any], run_ticket_id: int | None) -> Verdict:
    """Barrière dure : une action ne porte que sur le ticket de l'exécution."""
    if tool not in ACTIONS:
        return deny(f"Outil {tool} non autorisé : action refusée.")
    if run_ticket_id is None:
        return deny("Aucune action n'est permise pendant une vérification : action refusée.")
    if _int(tool_input.get("ticket_id")) != run_ticket_id:
        return deny(
            f"Tu traites le ticket {run_ticket_id} : aucune action sur un autre ticket. "
            "Action refusée."
        )
    return ALLOW


def evaluate(
    tool: str,
    tool_input: Mapping[str, Any],
    *,
    run_ticket_id: int | None,
    facts: OrderFacts | None = None,
    pending_on_ticket: bool = False,
) -> Verdict:
    """Verdict sur une action ; `facts` est requis pour un remboursement ou un renvoi."""
    verdict = check_ticket(tool, tool_input, run_ticket_id)
    if verdict.kind is not Kind.ALLOW:
        return verdict
    if tool == RESOLVE and pending_on_ticket:
        return deny(
            "Une action de ce ticket attend la validation d'un humain : le ticket doit rester "
            "ouvert. Ne le résous pas."
        )
    if tool not in GATED:
        return ALLOW

    if facts is None or facts.now is None:
        raise ValueError("faits ShopSim requis pour un remboursement ou un renvoi")
    order_id = tool_input.get("order_id")
    if facts.order_customer_id != facts.ticket_customer_id:
        return deny(
            f"La commande {order_id} n'appartient pas au client du ticket {run_ticket_id} : "
            "action refusée."
        )
    if tool == REFUND:
        requested = _int(tool_input.get("amount_cents")) or 0
        value = max(requested, facts.remaining_refundable_cents or 0)
    else:
        value = facts.order_total_cents

    reasons: list[Reason] = []
    if value > APPROVAL_THRESHOLD_CENTS:
        reasons.append(Reason.OVER_THRESHOLD)
    if is_risky_customer(facts.refund_dates, facts.now):
        reasons.append(Reason.RISKY_CUSTOMER)
    if reasons:
        return Verdict(Kind.HOLD, reasons=tuple(reasons), value_cents=value)
    return Verdict(Kind.ALLOW, value_cents=value)


def idempotency_key(tool: str, tool_input: Mapping[str, Any]) -> str:
    """Clé que le serveur MCP enverrait à ShopSim pour cette action (même fonction)."""
    ticket_id, order_id = int(tool_input["ticket_id"]), int(tool_input["order_id"])
    if tool == REFUND:
        return idempotency.refund_key(ticket_id, order_id)
    if tool == RESEND:
        return idempotency.resend_key(ticket_id, order_id)
    raise ValueError(f"pas de mise en attente pour {tool}")


async def gather_facts(tool: str, tool_input: Mapping[str, Any]) -> OrderFacts:
    """Relit dans ShopSim le client du ticket, la commande, le paiement (remboursement),
    les remboursements du client et l'heure de la simulation. Lève `ShopSimError`."""
    ticket_id, order_id = tool_input["ticket_id"], tool_input.get("order_id")
    if _int(order_id) is None:
        raise ValueError("order_id manquant ou invalide")
    ticket = await get_json(f"/tickets/{ticket_id}", f"Ticket {ticket_id} introuvable.")
    order = await get_json(f"/orders/{order_id}", f"Commande {order_id} introuvable.")
    remaining = None
    if tool == REFUND:
        payment = await get_json(
            f"/payments/{order_id}", f"Aucun paiement pour la commande {order_id}."
        )
        remaining = payment["amount_cents"] - payment["refunded_cents"]
    customer_id = ticket["customer_id"]
    refunds = await get_json(
        f"/customers/{customer_id}/refunds", f"Client {customer_id} introuvable."
    )
    clock = await get_json("/clock", "Horloge de ShopSim introuvable.")
    return OrderFacts(
        ticket_customer_id=customer_id,
        order_customer_id=order["customer_id"],
        order_total_cents=order["total_cents"],
        remaining_refundable_cents=remaining,
        refund_dates=tuple(parse_datetime(r["created_at"]) for r in refunds),
        now=parse_datetime(clock["now"]),
    )


def hold_message(verdict: Verdict, already_pending: bool) -> str:
    labels = " ; ".join(REASON_LABELS[r] for r in verdict.reasons)
    head = (
        "Action non exécutée : elle est déjà en attente de validation par un humain."
        if already_pending
        else f"Action non exécutée : elle attend la validation d'un humain ({labels})."
    )
    return (
        f"{head} N'annonce pas au client qu'elle est faite : réponds-lui que sa demande est "
        "transmise pour validation, sans lui en donner la raison, puis rends ta décision avec "
        "requires_human_approval à true. Ne réessaie pas cette action."
    )


def unverifiable_message(error: Exception) -> str:
    return (
        f"Vérification impossible avant l'action ({error}). Action non exécutée : ne "
        "l'annonce pas au client et escalade le ticket."
    )
