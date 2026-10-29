"""Faits cités par l'agent (`decision.facts_used`) confrontés aux vraies réponses d'outils (J5.2).

Le journal de la plateforme garde la réponse de chaque lecture (`TicketResult.reads`). Un fait
cité est :
- `verified` : une lecture réussie de cet outil contient ce champ avec cette valeur ;
- `unverifiable` : la réponse n'est pas gardée en entier, et jamais comptée contre l'agent :
  réponse tronquée au journal (au-delà de 4 000 caractères), message `error` d'une lecture en
  échec (seul champ qu'elle peut donner ; son texte n'est pas recopié dans les résultats), ou
  réponse d'une action exécutée (`refund_order`… : seuls ses paramètres sont gardés) ;
- `not_read` : l'outil n'a jamais été lu avec succès, ou l'action jamais exécutée (en attente,
  refusée, en échec) ;
- `mismatch` : l'outil a été lu, mais aucune réponse ne contient ce champ avec cette valeur.

Le champ est cherché à toute profondeur de la réponse (listes comprises), par son dernier nom
(`shipment.status` → `status`). Valeurs comparées sans tenir compte du type JSON (`21` et
`"21"`) ni de la casse ; une date citée seule correspond à un horodatage du même jour.

Équivalents d'un fait de justification (`EQUIVALENTS`) : l'agent ne calcule jamais une date ;
il cite le champ que l'outil donne (`days_since_delivery` plutôt que `delivered_at`), compté
comme le serveur MCP le compte (`elapsed_days`).
"""

import json
import re
from collections.abc import Callable, Collection, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from agentpilot.schemas import UsedFact
from evals.facts import FactValue
from evals.results import ActionCall, ReadCall
from mcp_server.computed import DAY, days_since, parse_datetime

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Check(StrEnum):
    VERIFIED = "verified"
    UNVERIFIABLE = "unverifiable"
    NOT_READ = "not_read"
    MISMATCH = "mismatch"


def short(name: str) -> str:
    """`mcp__shopsim__get_order` → `get_order` ; `shipment.status` → `status`."""
    return name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]


def normalized(value: Any) -> str:  # noqa: ANN401
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value).strip()
    try:
        return str(Decimal(text).normalize())
    except InvalidOperation:
        return text.casefold()


def same(cited: Any, read: Any) -> bool:  # noqa: ANN401
    if normalized(cited) == normalized(read):
        return True
    # Date citée seule, horodatage lu : même jour
    return (
        isinstance(cited, str)
        and isinstance(read, str)
        and ISO_DATE.fullmatch(cited) is not None
        and read.startswith(cited + "T")
    )


def _decoded(value: Any) -> Any:  # noqa: ANN401
    """Texte JSON (réponse MCP en blocs de texte) décodé ; le reste tel quel."""
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def values(response: Any, field: str) -> Iterator[Any]:  # noqa: ANN401
    """Toutes les valeurs du champ `field`, à toute profondeur de la réponse."""
    response = _decoded(response)
    if isinstance(response, dict):
        for key, value in response.items():
            if key == field:
                yield value
            yield from values(value, field)
    elif isinstance(response, list):
        for item in response:
            yield from values(item, field)


def truncated(response: Any) -> bool:  # noqa: ANN401
    """Réponse coupée par le journal (`agentpilot.hooks.clip`) : illisible au-delà."""
    return isinstance(response, dict) and set(response) == {"truncated"}


def check(fact: UsedFact, reads: Sequence[ReadCall], actions: Sequence[ActionCall] = ()) -> Check:
    tool, field = short(fact.tool), short(fact.field)
    if any(a.tool == tool for a in actions):
        # Réponse d'une action : non gardée ; elle n'existe que si l'action a été exécutée
        executed = any(a.tool == tool and a.outcome == "succeeded" for a in actions)
        return Check.UNVERIFIABLE if executed else Check.NOT_READ
    done = [r for r in reads if r.tool == tool and r.outcome == "succeeded"]
    if not done:
        failed = any(r.tool == tool and r.outcome == "failed" for r in reads)
        return Check.UNVERIFIABLE if failed and field == "error" else Check.NOT_READ
    if any(same(fact.value, v) for r in done for v in values(r.response, field)):
        return Check.VERIFIED
    if any(truncated(r.response) for r in done):
        return Check.UNVERIFIABLE
    return Check.MISMATCH


def elapsed_days(day: str, now: datetime, reads: Sequence[ReadCall] | None) -> frozenset[int]:
    """Jours depuis une livraison du jour `day`, comptés comme l'outil les donne
    (`days_since_delivery` : jours entamés, `mcp_server.computed.days_since`).

    Heure de livraison lue dans les réponses de `get_shipment` quand l'une donne ce jour-là ;
    sinon (la vérité terrain ne garde que la date) toute heure du jour est admise, soit deux
    valeurs au plus.
    """
    moments = [
        parse_datetime(v)
        for r in reads or []
        if r.tool == "get_shipment" and r.outcome == "succeeded"
        for v in values(r.response, "delivered_at")
        if isinstance(v, str) and v.startswith(day + "T")
    ]
    if not moments:
        start = datetime.combine(date.fromisoformat(day), time(), tzinfo=UTC)
        moments = [start, start + DAY - timedelta(microseconds=1)]
    return frozenset(days_since(moment, now) for moment in moments)


def _days_since(
    value: FactValue, now: datetime, reads: Sequence[ReadCall] | None
) -> Collection[FactValue]:
    return elapsed_days(str(value), now, reads)


def _same_value(
    value: FactValue, now: datetime, reads: Sequence[ReadCall] | None
) -> Collection[FactValue]:
    return (value,)


# Fait de justification → (outil, champ, valeurs admises d'après la vérité terrain, la date
# simulée et les lectures) acceptés dans `facts_used`. Sans entrée : cherché dans les textes
# seulement.
Equivalent = tuple[
    str, str, Callable[[FactValue, datetime, Sequence[ReadCall] | None], Collection[FactValue]]
]
EQUIVALENTS: dict[str, list[Equivalent]] = {
    "delivered_at": [
        ("get_shipment", "delivered_at", _same_value),
        ("get_shipment", "days_since_delivery", _days_since),
    ],
    "refund_amount_cents": [
        ("get_payment", "remaining_refundable_cents", _same_value),
        ("get_payment", "amount_cents", _same_value),
    ],
    "carrier": [("get_shipment", "carrier", _same_value)],
    "tracking_number": [("get_shipment", "tracking_number", _same_value)],
    "estimated_delivery": [("get_shipment", "estimated_delivery", _same_value)],
}


def cited(
    key: str,
    value: FactValue,
    used: Sequence[UsedFact],
    now: datetime,
    reads: Sequence[ReadCall] | None = None,
) -> bool:
    """Le fait de justification `key` figure dans `used`, sous un de ses équivalents."""
    for tool, field, expected in EQUIVALENTS.get(key, []):
        wanted = expected(value, now, reads)
        for fact in used:
            if (
                short(fact.tool) == tool
                and short(fact.field) == field
                and any(same(w, fact.value) for w in wanted)
            ):
                return True
    return False
