"""Règles métier du support Lumen : seule source des seuils et des délais.

Le seed s'en sert pour calculer la vérité terrain. La skill de l'agent Support
(`agents/support/skills/refund-policy/SKILL.md`) reprend ces mêmes règles ; le test
`agentpilot/tests/test_refund_policy_skill.py` vérifie que ses valeurs restent identiques.

Produit défectueux : le renvoi n'est pas limité aux 30 jours du remboursement
(garantie légale de conformité).
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from evals.ground_truth import ExpectedAction

# Remboursement possible jusqu'à 30 jours après la livraison
REFUND_WINDOW = timedelta(days=30)
# Au-delà de 50 € (montant remboursé ou valeur renvoyée) : validation humaine
APPROVAL_THRESHOLD_CENTS = 5_000
# Client à risque : au moins 3 remboursements sur les 90 derniers jours
RISK_WINDOW = timedelta(days=90)
RISK_MIN_REFUNDS = 3


def within_refund_window(delivered_at: datetime, now: datetime) -> bool:
    return now - delivered_at <= REFUND_WINDOW


def is_risky_customer(refund_dates: Iterable[datetime], now: datetime) -> bool:
    recent = sum(timedelta(0) <= now - at <= RISK_WINDOW for at in refund_dates)
    return recent >= RISK_MIN_REFUNDS


def decide(
    intended_action: ExpectedAction,
    *,
    value_cents: int | None = None,
    risky: bool = False,
    trap: bool = False,
    within_window: bool = True,
) -> tuple[ExpectedAction, bool]:
    """Action attendue et besoin de validation humaine, dans cet ordre de priorité :

    1. cas piège : escalade (traitée par un humain) ;
    2. remboursement hors délai : refus poli, sans validation ;
    3. sinon l'action prévue, validée par un humain si elle vaut plus que le
       seuil ou si le client est à risque (remboursement et renvoi seulement).
    """
    if trap:
        return ExpectedAction.ESCALATE, True
    if intended_action == ExpectedAction.REFUND and not within_window:
        return ExpectedAction.REFUSE, False
    if intended_action in (ExpectedAction.REFUND, ExpectedAction.RESEND):
        over_threshold = value_cents is not None and value_cents > APPROVAL_THRESHOLD_CENTS
        return intended_action, over_threshold or risky
    return intended_action, False
