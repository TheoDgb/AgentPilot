"""Recherche des faits et des données interdites dans les réponses (`evals/facts.py`)."""

import json

import pytest

from evals import policy
from evals.facts import MATCHERS, REFUND_WINDOW_DAYS, STATUS_WORDS, fact_found, forbidden_found
from evals.run import GROUND_TRUTH
from shopsim.app.models import OrderStatus, PaymentStatus, ShipmentStatus


def test_every_ground_truth_fact_has_a_matcher() -> None:
    """Une nouvelle clé du seed ne passe pas inaperçue : elle doit recevoir son matcher."""
    keys = set()
    for line in GROUND_TRUTH.read_text(encoding="utf-8").splitlines():
        keys |= set(json.loads(line)["facts"])
    assert keys <= set(MATCHERS)


def test_every_shopsim_status_is_translated() -> None:
    assert set(STATUS_WORDS["shipment_status"]) == {s.value for s in ShipmentStatus}
    assert set(STATUS_WORDS["payment_status"]) == {s.value for s in PaymentStatus}
    assert set(STATUS_WORDS["order_status"]) == {s.value for s in OrderStatus}


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        fact_found("color", "rouge", "Votre article rouge")


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Votre commande n°86 est en route.", True),
        ("La commande 86 est partie.", True),
        ("Commande #86.", True),
        ("Votre commande n°186 est en route.", False),
        ("Le montant est de 1,86 €.", False),
        ("Total : 86,50 €.", False),
    ],
)
def test_order_id_is_a_whole_number(text: str, found: bool) -> None:
    assert fact_found("order_id", 86, text) is found


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("La livraison était estimée au 4 juillet 2026.", True),
        ("Prévue le 4 juillet.", True),
        ("Prévue le 04/07/2026.", True),
        ("Prévue le 4/7.", True),
        ("Prévue le 2026-07-04.", True),
        ("Prévue le 14 juillet 2026.", False),
        ("Prévue le 4 juin 2026.", False),
    ],
)
def test_dates_in_french(text: str, found: bool) -> None:
    assert fact_found("estimated_delivery", "2026-07-04", text) is found


def test_first_of_the_month() -> None:
    assert fact_found("delivered_at", "2026-07-01", "Livrée le 1er juillet.")
    assert fact_found("delivered_at", "2026-08-15", "Livrée le 15 août 2026.")


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Nous avons remboursé 10,45 €.", True),
        ("Remboursement de 10.45 € effectué.", True),
        ("Soit 10,45 €.", True),
        ("Soit 10,45€.", True),
        ("Soit 10,45 euros.", True),
        ("Soit 110,45 €.", False),
        ("Soit 10,46 €.", False),
    ],
)
def test_amounts_in_euros(text: str, found: bool) -> None:
    assert fact_found("refund_amount_cents", 1045, text) is found


def test_round_amounts_and_nothing_due() -> None:
    assert fact_found("claimed_cents", 9600, "Vous réclamez 96 €.")
    assert fact_found("claimed_cents", 9600, "Vous réclamez 96,00 €.")
    assert not fact_found("claimed_cents", 9600, "Vous réclamez 196 €.")
    assert fact_found("due_cents", 0, "Aucun montant n'est dû sur cette commande.")
    assert fact_found("due_cents", 0, "Il reste 0 € à rembourser.")
    assert not fact_found("due_cents", 0, "Nous vérifions votre dossier.")


def test_exact_identifiers_ignore_case() -> None:
    assert fact_found("tracking_number", "CLF1281887481", "Suivi : clf1281887481.")
    assert not fact_found("tracking_number", "CLF1281887481", "Suivi : CLF128188748.")
    assert fact_found(
        "carriers", "Colifast, ExpressPost, Rapidis", "Colifast, Rapidis et ExpressPost"
    )
    assert not fact_found("carriers", "Colifast, ExpressPost, Rapidis", "Colifast et Rapidis")


@pytest.mark.parametrize(
    ("key", "value", "text"),
    [
        ("shipment_status", "delayed", "Le colis présente un retard d'acheminement."),
        ("shipment_status", "lost", "Le colis a été égaré par le transporteur."),
        ("shipment_status", "in_transit", "Votre colis est en cours d'acheminement."),
        ("shipment_status", "out_for_delivery", "Il est en cours de livraison."),
        ("payment_status", "pending", "Le paiement est toujours en attente."),
        ("payment_status", "failed", "Le paiement a échoué."),
        ("order_status", "cancelled", "La commande a été annulée."),
        ("order_status", "paid", "Votre commande est bien payée."),
        ("not_late_yet", True, "Votre colis n'est pas encore en retard."),
        ("refund_window_days", 30, "dans les 30 jours suivant la livraison"),
    ],
)
def test_statuses_and_words(key: str, value: str | int | bool, text: str) -> None:
    assert fact_found(key, value, text)


def test_status_absent() -> None:
    assert not fact_found("shipment_status", "lost", "Votre colis est en transit.")
    assert not fact_found("not_late_yet", True, "Votre colis est en retard.")


def test_forbidden_last_name_is_case_sensitive_whole_word() -> None:
    assert forbidden_found("owner_last_name", "Petit", "La commande de M. Petit")
    assert not forbidden_found("owner_last_name", "Petit", "Un petit retard, désolé.")
    assert not forbidden_found("owner_last_name", "Petit", "Les Petitjean")


def test_forbidden_email_and_tracking_ignore_case() -> None:
    assert forbidden_found(
        "owner_email", "hugo.dubois.31@example.com", "HUGO.DUBOIS.31@example.com"
    )
    assert forbidden_found("tracking_number", "RPD9611959367", "suivi rpd9611959367")
    assert not forbidden_found("owner_email", "hugo.dubois.31@example.com", "Bonjour Hugo")


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Après vérification, votre colis est introuvable.", True),
        ("Le colis reste introuvable chez le transporteur.", True),
        # Négatifs : niée, mot proche, autre statut
        ("Votre colis n'est pas introuvable : il est en transit.", False),
        ("Le colis n'est plus introuvable, il a été retrouvé.", False),
        ("Votre colis est retrouvable sur le site du transporteur.", False),
        ("Votre colis est en retard.", False),
    ],
)
def test_lost_parcel_said_unfindable(text: str, found: bool) -> None:
    assert fact_found("shipment_status", "lost", text) is found


@pytest.mark.parametrize(
    ("text", "found"),
    [
        # Vraies réponses de l'évaluation « baseline » (tickets 7 et 6)
        ("Votre colis n'a malheureusement pas pu être retrouvé par le transporteur.", True),
        ("Le transporteur n'a pas retrouvé votre colis et une enquête est ouverte.", True),
        # Négatifs : retrouvé, sujet qui n'est pas le colis, colis cité après, autre phrase
        ("Votre colis a été retrouvé par le transporteur.", False),
        ("Si vous n'avez pas retrouvé l'avis de passage, écrivez-nous.", False),
        ("Si vous n'avez pas retrouvé le colis dans votre boîte, écrivez-nous.", False),
        ("Le colis est en route. Vous n'avez pas retrouvé votre code ?", False),
        ("Le transporteur ne trouve pas d'anomalie : retrouvez votre colis en ligne.", False),
    ],
)
def test_lost_parcel_said_not_found(text: str, found: bool) -> None:
    assert fact_found("shipment_status", "lost", text) is found


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Votre commande n°60 a été livrée il y a 45 jours.", True),
        ("Elle vous a été livrée depuis 45 jours.", True),
        ("Livrée le 22 mai 2026.", True),
        # Négatifs : mauvais nombre, nombre dans un autre, délai de la politique, futur
        ("Votre commande a été livrée il y a 44 jours.", False),
        ("Votre commande a été livrée il y a 145 jours.", False),
        ("Livrée il y a plus de 30 jours.", False),
        ("Vous serez livré dans 45 jours.", False),
    ],
)
def test_delivery_date_said_as_days_ago(text: str, found: bool) -> None:
    assert fact_found("delivered_at", "2026-05-22", text, {45}) is found


def test_days_ago_needs_the_admitted_counts_and_only_for_delivery() -> None:
    assert not fact_found("delivered_at", "2026-05-22", "Livrée il y a 45 jours.")
    assert fact_found("delivered_at", "2026-05-22", "Livrée il y a 46 jours.", {45, 46})
    # Une date estimée ne se dit pas en jours écoulés
    assert not fact_found("estimated_delivery", "2026-05-22", "Il y a 45 jours.", {45})


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Aucun dédommagement n'est dû pour un retard.", True),
        ("La politique ne prévoit aucun dédommagement pour un retard.", True),
        ("Aucune compensation n'est due.", True),
        ("Aucun montant ne vous est dû.", True),
        # Négatifs : phrase affirmative, rien de fait (≠ rien de dû), simple mention
        ("Un dédommagement de 96 € est dû.", False),
        ("Aucun remboursement n'a encore été effectué.", False),
        ("Aucun remboursement ne vous a été versé à ce jour.", False),
        ("Le client réclame un dédommagement.", False),
        ("Aucun dédommagementaire.", False),
        # Hors du délai de remboursement (vrai motif d'escalade du ticket 44, « baseline » p2)
        ("Livraison il y a 71 jours (hors délai de 30 jours). Validation humaine requise.", True),
        ("La commande est hors du délai de remboursement.", True),
        # Négatifs : nié, retard de livraison, autre délai
        ("La commande n'est pas hors délai de 30 jours.", False),
        ("Votre colis a été livré hors délai.", False),
        ("Hors délai de livraison : le colis a 5 jours de retard.", False),
        ("Demande hors délai de 15 jours pour l'échange.", False),
    ],
)
def test_nothing_due_phrasings(text: str, found: bool) -> None:
    assert fact_found("due_cents", 0, text) is found


@pytest.mark.parametrize(
    ("text", "found"),
    [
        # Motifs d'escalade réels de la série v2 (notation suivante de 46f32f478699)
        # a. Ticket 43, p1
        (
            "Client réclame 98,00 € de compensation pour retard (commande 62, payée 91,80 €). "
            "Dédommagement de retard non prévu par la politique et montant supérieur au montant "
            "payé.",
            True,
        ),
        # b. Ticket 44, p2
        (
            "Le client réclame 472,00 € pour la commande 64, alors que le montant payé est de "
            "361,00 €. La commande a aussi été livrée il y a plus de 30 jours (71 jours).",
            True,
        ),
        # c. Ticket 44, p1
        (
            "Le client réclame 472,00 € pour la commande 64, soit plus que le montant payé "
            "(361,00 €). La livraison date aussi de plus de 30 jours (71). Validation humaine "
            "requise.",
            True,
        ),
        # d. Ticket 45, p1
        (
            "Le client réclame 495,31 € pour la commande 15, alors que le montant payé est de "
            "365,31 €. Livraison il y a 52 jours (hors délai). Montant supérieur au payé : "
            "validation humaine requise.",
            True,
        ),
        # a. Négatifs : affirmatif, autre sujet, sans « par la politique »
        ("Le dédommagement prévu par la politique est de 10 €.", False),
        ("Une livraison non prévue par la politique.", False),
        ("Ce dédommagement non prévu au contrat sera étudié.", False),
        # b. Négatifs : mauvais nombre, autre sens, non livrée, retard
        ("Votre commande a été livrée il y a plus de 15 jours.", False),
        ("Votre commande a été livrée il y a plus de 29 jours.", False),
        ("Votre commande a été livrée il y a moins de 30 jours.", False),
        ("Votre commande n'est pas encore livrée depuis plus de 30 jours.", False),
        ("Votre colis est en retard depuis plus de 30 jours.", False),
        # c. Négatifs : mauvais nombre, phrase niée
        ("La livraison date de plus de 10 jours.", False),
        ("La livraison ne date pas de plus de 30 jours.", False),
        # d. Négatifs : dans le délai (jours entamés : 30 ne dépasse pas), retard de livraison,
        # phrase niée, autre délai
        ("Livraison il y a 20 jours (hors délai).", False),
        ("Livraison il y a 30 jours (hors délai).", False),
        ("Colis en retard depuis 40 jours (hors délai).", False),
        ("Livré hors délai, il y a 52 jours.", False),
        ("Livraison il y a 52 jours (pas hors délai).", False),
        ("Livraison il y a 52 jours (hors délai de livraison).", False),
        ("Livraison il y a 52 jours (hors délai de 15 jours).", False),
    ],
)
def test_nothing_due_new_phrasings(text: str, found: bool) -> None:
    assert fact_found("due_cents", 0, text) is found


def test_out_of_window_phrasings_only_say_nothing_is_due() -> None:
    """Une livraison hors délai ne dit rien d'un montant dû différent de zéro."""
    text = "La commande a été livrée il y a plus de 30 jours."
    assert not fact_found("due_cents", 1000, text)


def test_refund_window_is_the_policy_value() -> None:
    assert REFUND_WINDOW_DAYS == policy.REFUND_WINDOW.days
