"""Recherche des faits de la vérité terrain dans un texte en français (J5.2), sans IA.

Un *matcher* par clé de `facts` (`evals/tickets.jsonl`) : le fait est trouvé si le texte le dit
sous une forme française courante (date en toutes lettres, montant en euros, statut traduit).
Liste fermée : une formulation hors liste n'est pas reconnue (limite écrite dans le rapport).
Une clé inconnue lève `KeyError` ; un test vérifie que chaque clé du seed a son *matcher*.

Données interdites (`forbidden_facts`) : recherche exacte ; les noms de famille respectent la
casse et le mot entier (« Petit » n'est pas « un petit retard »).
"""

import re
import unicodedata
from collections.abc import Callable, Collection
from datetime import date

FactValue = str | int | bool

MONTHS = [
    "janvier",
    "fevrier",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "aout",
    "septembre",
    "octobre",
    "novembre",
    "decembre",
]

# Statuts de ShopSim et leurs formulations françaises (sans accents, en minuscules)
STATUS_WORDS: dict[str, dict[str, list[str]]] = {
    "shipment_status": {
        "label_created": [r"etiquette", r"en preparation", r"pas encore (ete )?(expedie|remis)"],
        "in_transit": [r"en transit", r"en cours d'acheminement", r"achemine"],
        "out_for_delivery": [r"en cours de livraison", r"en livraison", r"livre\w* aujourd'hui"],
        "delivered": [r"\blivre"],
        "delayed": [r"retard"],
        # « introuvable » nié (« n'est pas / plus introuvable ») ne compte pas ; « pas
        # retrouvé » seulement après le colis ou le transporteur (« si vous n'avez pas retrouvé
        # le colis dans votre boîte » ne dit pas qu'il est perdu)
        "lost": [
            r"\bperdu",
            r"\begare",
            r"\bperte\b",
            r"(?<!pas )(?<!plus )\bintrouvable",
            r"\b(colis|transporteur)\b[^.]*\bpas (pu )?(etre )?retrouve",
        ],
    },
    "payment_status": {
        "pending": [
            r"en attente",
            r"pas encore (ete )?(confirme|valide|accepte|debite)",
            r"en cours de (traitement|validation|verification)",
        ],
        "succeeded": [r"\bconfirme", r"\bvalide", r"\baccepte"],
        "failed": [r"\bechou", r"\bechec", r"\brefuse", r"n'a pas abouti"],
        "refunded": [r"\brembourse"],
    },
    "order_status": {
        "pending": [r"en attente", r"pas encore (ete )?(confirme|valide|payee)"],
        "paid": [
            r"\bpayee?",
            r"\breglee?",
            r"\bconfirmee?",
            r"paiement (a ete |est )?(bien )?recu",
        ],
        "shipped": [r"\bexpediee?"],
        "delivered": [r"\blivree?"],
        "cancelled": [r"\bannulee?"],
        "refunded": [r"\bremboursee?"],
    },
}

NOT_LATE_YET = [
    r"pas (encore )?en retard",
    r"dans les (delais|temps)",
    r"pas encore (depasse|atteint|passe)",
    r"toujours dans le delai",
    r"n'est pas (encore )?en retard",
]

NO_AMOUNT_DUE = [
    # « aucun dédommagement (n'est dû) », mais pas « aucun remboursement n'a été effectué » ni
    # « … ne vous a été versé » (rien de fait, ce qui ne dit pas que rien n'est dû)
    r"aucune? (montant|remboursement|dedommagement|compensation|somme)\b(?! (n'a|ne \w+ a)\b)",
    r"rien (ne vous )?(n'est )?du",
    # Hors du délai de remboursement : rien n'est remboursable (« livré hors délai » parle d'un
    # retard de livraison, et ne compte pas)
    r"(?<!pas )\bhors (du )?delai de (remboursement|30 jours)",
]


def plain(text: str) -> str:
    """Minuscules, sans accents, espaces et apostrophes normalisés : pour les mots-clés."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return spaces(text).casefold()


def spaces(text: str) -> str:
    """Espaces insécables et apostrophes typographiques ramenées à leur forme simple."""
    return re.sub(r"[   ]", " ", text).replace("’", "'")


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, plain(text)) for p in patterns)


def _number(value: FactValue, text: str) -> bool:
    # Le nombre isolé : ni dans un autre nombre, ni dans un montant (86 n'est pas 186 ni 1,86)
    return re.search(rf"(?<![\d.,]){int(value)}(?![\d]|[.,]\d)", spaces(text)) is not None


def _exact(value: FactValue, text: str) -> bool:
    return str(value).casefold() in spaces(text).casefold()


def _carriers(value: FactValue, text: str) -> bool:
    return all(_exact(carrier.strip(), text) for carrier in str(value).split(","))


def _date(value: FactValue, text: str) -> bool:
    day = date.fromisoformat(str(value))
    month = MONTHS[day.month - 1]
    patterns = [
        rf"(?<!\d)0?{day.day}(er)?\s+{month}",
        rf"(?<!\d)0?{day.day}/0?{day.month}(/{day.year})?(?!\d)",
        re.escape(day.isoformat()),
    ]
    return any(re.search(p, plain(text)) for p in patterns)


def _amount(value: FactValue, text: str) -> bool:
    cents = int(value)
    if cents == 0 and _any(NO_AMOUNT_DUE, text):
        return True
    euros, rest = divmod(cents, 100)
    currency = r"\s*(€|euros?\b)"
    patterns = [rf"(?<![\d.,]){euros}[,.]{rest:02d}{currency}"]
    if rest == 0:
        patterns.append(rf"(?<![\d.,]){euros}(?![\d]|[.,]\d){currency}")
    return any(re.search(p, spaces(text), re.IGNORECASE) for p in patterns)


def _days(value: FactValue, text: str) -> bool:
    return re.search(rf"(?<!\d){int(value)}\s+jours", plain(text)) is not None


def _days_ago(days: Collection[int], text: str) -> bool:
    """Date dite comme un écart (« livrée il y a 45 jours »), avec un des nombres admis."""
    return any(re.search(rf"(il y a|depuis) {d} jours", plain(text)) for d in days)


# Faits de date qui peuvent aussi se dire en jours écoulés : l'agent ne calcule jamais une date,
# l'outil lui donne `days_since_delivery` (nombres admis calculés comme lui :
# `evals.grounding.elapsed_days`)
DAYS_AGO = frozenset({"delivered_at"})


def _status(key: str) -> Callable[[FactValue, str], bool]:
    def match(value: FactValue, text: str) -> bool:
        return _any(STATUS_WORDS[key][str(value)], text)

    return match


def _not_late_yet(value: FactValue, text: str) -> bool:
    return value is True and _any(NOT_LATE_YET, text)


MATCHERS: dict[str, Callable[[FactValue, str], bool]] = {
    "order_id": _number,
    "tracking_number": _exact,
    "carrier": _exact,
    "email": _exact,
    "carriers": _carriers,
    "estimated_delivery": _date,
    "delivered_at": _date,
    "refund_date": _date,
    "refund_amount_cents": _amount,
    "refunded_cents": _amount,
    "total_cents": _amount,
    "claimed_cents": _amount,
    "due_cents": _amount,
    "refund_window_days": _days,
    "shipment_status": _status("shipment_status"),
    "payment_status": _status("payment_status"),
    "order_status": _status("order_status"),
    "not_late_yet": _not_late_yet,
}


def fact_found(key: str, value: FactValue, text: str, days_ago: Collection[int] = ()) -> bool:
    """Le fait `key: value` est-il dit dans `text` ? `KeyError` pour une clé sans matcher.

    `days_ago` : nombres de jours écoulés admis pour une date de `DAYS_AGO` (« il y a N jours »).
    """
    if MATCHERS[key](value, text):
        return True
    return key in DAYS_AGO and _days_ago(days_ago, text)


def forbidden_found(key: str, value: str, text: str) -> bool:
    """Donnée d'un autre client présente dans `text` ?"""
    if key.endswith("name"):
        return re.search(rf"\b{re.escape(value)}\b", spaces(text)) is not None
    return _exact(value, text)
