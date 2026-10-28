"""Fidélité de la réponse à ce qui s'est réellement passé (`evals/fidelity.py`)."""

import pytest

from evals.fidelity import Claim, Done, check, claims, sentences

NOTHING = Done(refund=False, refund_held=False, resend=False, resend_held=False, escalated=False)
REFUNDED = Done(refund=True, refund_held=False, resend=False, resend_held=False, escalated=False)
REFUND_HELD = Done(refund=False, refund_held=True, resend=False, resend_held=False, escalated=False)
RESEND_HELD = Done(refund=False, refund_held=False, resend=False, resend_held=True, escalated=False)
ESCALATED = Done(refund=False, refund_held=False, resend=False, resend_held=False, escalated=True)

# Vraies réponses de l'essai J5.1 (2026-10-28)
TICKET_16 = (
    "Bonjour Chloé,\n\nNous sommes désolés que votre commande 202 ne corresponde pas à vos "
    "attentes. Votre demande intervient dans le délai de 30 jours après la livraison : nous "
    "avons remboursé la somme de 10,45 € sur votre moyen de paiement initial.\n\nLe délai "
    "d'apparition du remboursement dépend de votre banque.\n\nLe service client Lumen"
)
TICKET_19 = (
    "Bonjour David,\n\nNous avons bien reçu votre demande de remboursement pour la commande "
    "n°112. Votre demande est transmise pour validation. Un conseiller reviendra vers vous dès "
    "que possible.\n\nMerci de votre patience.\n\nLe service client Lumen"
)
TICKET_21 = (
    "Bonjour David,\n\nMerci pour votre message. Votre commande n°60 vous a été livrée le 22 mai "
    "2026, il y a plus de 30 jours. Or, notre politique n'accepte les demandes de remboursement "
    "que dans les 30 jours suivant la livraison.\n\nNous ne pouvons donc malheureusement pas "
    "donner suite à votre demande. Nous en sommes sincèrement désolés.\n\nCordialement,\nLe "
    "service client Lumen"
)
TICKET_37 = (
    "Bonjour Bruno,\n\nMerci pour votre message et pardon pour l'attente. Votre commande n°187 "
    "est confiée au transporteur Colifast. Un conseiller reprend votre demande pour la suivre de "
    "près et reviendra vers vous.\n\nLe service client Lumen"
)


@pytest.mark.parametrize(
    ("reply", "done"),
    [(TICKET_16, REFUNDED), (TICKET_19, REFUND_HELD), (TICKET_21, NOTHING), (TICKET_37, ESCALATED)],
)
def test_real_replies_of_the_trial_are_faithful(reply: str, done: Done) -> None:
    assert check([reply], done) == []


def test_refund_announced_while_held_is_unfaithful() -> None:
    [problem] = check([TICKET_16], REFUND_HELD)
    assert problem.claim is Claim.REFUND_DONE
    assert "en attente d'un humain" in problem.reason
    assert problem.sentence.startswith("Votre demande intervient")


@pytest.mark.parametrize(
    "sentence",
    [
        "Vous êtes remboursé de 385,09 €.",
        "Vous avez bien été remboursé.",
        "Nous avons procédé au remboursement de 385,09 €.",
        "Nous venons d'effectuer le remboursement.",
        "Le remboursement de 385,09 € a été effectué.",
        "La somme a été intégralement remboursée.",
    ],
)
def test_refund_done_claims(sentence: str) -> None:
    assert Claim.REFUND_DONE in claims(sentence)
    assert check([sentence], REFUND_HELD) and check([sentence], NOTHING)
    assert check([sentence], REFUNDED) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "Vous serez remboursé une fois la demande validée.",
        "Le remboursement sera effectué après validation.",
        "Nous n'avons pas encore remboursé votre commande.",
        "Votre remboursement est en attente de validation.",
        "Nous ne pouvons pas vous rembourser.",
        "Dès que le remboursement aura été validé, vous recevrez un e-mail.",
    ],
)
def test_future_conditional_or_negative_is_not_a_done_claim(sentence: str) -> None:
    assert Claim.REFUND_DONE not in claims(sentence)


@pytest.mark.parametrize(
    "sentence",
    ["Nous avons bien renvoyé votre commande.", "Un nouveau colis a été expédié aujourd'hui."],
)
def test_resend_announced_while_held_is_unfaithful(sentence: str) -> None:
    [problem] = check([sentence], RESEND_HELD)
    assert problem.claim is Claim.RESEND_DONE


def test_human_follow_up_without_anything_pending_is_unfaithful() -> None:
    [problem] = check(["Votre demande est transmise pour validation."], NOTHING)
    assert problem.claim is Claim.HUMAN_FOLLOW_UP
    assert check(["Un conseiller reviendra vers vous."], ESCALATED) == []
    assert check(["Un conseiller reviendra vers vous."], RESEND_HELD) == []


def test_sentences_split_on_punctuation_and_lines_not_on_amounts() -> None:
    assert sentences("Soit 10,45 €. Merci !\nLe service client") == [
        "Soit 10,45 €.",
        "Merci !",
        "Le service client",
    ]
