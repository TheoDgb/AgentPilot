"""Décision humaine sur une action en attente : approbation, refus, suite donnée au client.

L'action approuvée est exécutée par le code de la plateforme, jamais par l'agent, avec les
paramètres exacts de l'agent et la clé d'idempotence enregistrée par le hook. Deux barrières
contre une double exécution :

- la décision est prise par un `UPDATE … WHERE status = 'pending'` : un double clic ou un
  second approbateur ne trouve plus l'action en attente et n'exécute rien ;
- ShopSim reconnaît la clé d'idempotence : un rejeu ne rembourse ni ne renvoie deux fois.

Comme les hooks, ce code lit et écrit ShopSim par le client HTTP du serveur MCP : c'est la
plateforme qui agit, sur décision d'un humain, pas un agent.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from agentpilot.approval import REFUND, RESEND
from agentpilot.models import PendingAction, PendingStatus
from mcp_server import idempotency
from mcp_server.shopsim_client import ShopSimError, ShopSimUnavailable, get_json, post_json

NAME_MAX = 100
NOTE_MAX = 500
MESSAGE_MAX = 5_000
SIGNATURE = "Le service client Lumen"
# Une action approuvée mais pas encore exécutée n'est pas terminée : le ticket reste ouvert
UNFINISHED = (PendingStatus.PENDING, PendingStatus.APPROVED)


class Notice(StrEnum):
    """Ce qui s'est passé, affiché après la redirection vers la page de l'action."""

    EXECUTED = "executed"
    IMPOSSIBLE = "impossible"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    ALREADY_DECIDED = "already_decided"
    NOT_RETRYABLE = "not_retryable"
    MESSAGE_SENT = "message_sent"
    MESSAGE_SKIPPED = "message_skipped"
    MESSAGE_ALREADY_SENT = "message_already_sent"
    RESOLVED = "resolved"
    RESOLVE_BLOCKED = "resolve_blocked"
    FOLLOW_UP_FAILED = "follow_up_failed"


class InvalidInput(ValueError):
    """Saisie refusée (nom ou motif manquant, texte trop long) : rien n'a changé."""


def _now() -> datetime:
    # Heure réelle de la plateforme, pas celle de la simulation
    return datetime.now(UTC)


def eur(cents: int) -> str:
    """12345 → « 123,45 € »."""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", " ") + f",{rest:02d} €"


def _cents(value: Any) -> int | None:  # noqa: ANN401
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def amount_mismatch(action: PendingAction, decision: Mapping[str, Any] | None) -> str | None:
    """Écart entre le montant de l'agent et la valeur lue par le hook dans ShopSim, ou None.

    Remboursement : montant demandé (celui qui sera exécuté) contre reste remboursable.
    Renvoi : l'outil ne prend pas de montant, on compare celui de la décision de l'agent à
    la valeur de la commande.
    """
    if action.tool == REFUND:
        requested = _cents(action.params.get("amount_cents"))
        remaining = _cents(action.facts.get("remaining_refundable_cents"))
        if requested is not None and remaining is not None and requested != remaining:
            return f"Montant demandé : {eur(requested)} — reste dû : {eur(remaining)}"
    elif action.tool == RESEND:
        declared = _cents((decision or {}).get("amount_cents"))
        total = _cents(action.facts.get("order_total_cents"))
        if declared is not None and total is not None and declared != total:
            return (
                f"Valeur annoncée par l'agent : {eur(declared)} — "
                f"valeur de la commande : {eur(total)}"
            )
    return None


def default_message(action: PendingAction, approved: bool, first_name: str | None) -> str:
    """Message prérempli pour le client, sans la raison interne de la validation."""
    hello = f"Bonjour {first_name}," if first_name else "Bonjour,"
    order = action.params.get("order_id")
    if not approved:
        body = (
            "Après examen, nous ne pouvons malheureusement pas donner suite à votre demande "
            f"concernant la commande n°{order}. Un conseiller reste à votre disposition si "
            "vous souhaitez en parler."
        )
    elif action.tool == REFUND:
        amount = _cents(action.params.get("amount_cents")) or 0
        body = (
            f"Votre demande a été validée : nous avons remboursé {eur(amount)} sur la commande "
            f"n°{order}. Le montant apparaîtra sur votre moyen de paiement sous quelques jours."
        )
    else:
        body = (
            "Votre demande a été validée : nous vous renvoyons gratuitement les articles de la "
            f"commande n°{order}. Vous recevrez le suivi du colis dès son expédition."
        )
    return f"{hello}\n\n{body}\n\n{SIGNATURE}"


def failure_message(first_name: str | None) -> str:
    """Message prérempli quand l'action approuvée n'a pas pu être exécutée."""
    hello = f"Bonjour {first_name}," if first_name else "Bonjour,"
    return (
        f"{hello}\n\nNous n'avons pas pu donner suite à votre demande en l'état. Un conseiller "
        f"revient vers vous rapidement.\n\n{SIGNATURE}"
    )


def _text(value: str, field: str, maximum: int, *, required: bool) -> str | None:
    # Les navigateurs envoient les retours à la ligne d'un formulaire en \r\n
    text = value.replace("\r\n", "\n").strip()
    if not text:
        if required:
            raise InvalidInput(f"Le champ « {field} » est obligatoire.")
        return None
    if len(text) > maximum:
        raise InvalidInput(f"Le champ « {field} » dépasse {maximum} caractères.")
    return text


# --- Base ------------------------------------------------------------------------------


def _claim(
    factory: sessionmaker[Session],
    action_id: uuid.UUID,
    status: PendingStatus,
    name: str,
    note: str | None,
) -> bool:
    """Prend la décision si l'action est encore en attente ; False si quelqu'un l'a déjà prise."""
    with factory() as session:
        claimed = session.execute(
            update(PendingAction)
            .where(PendingAction.id == action_id, PendingAction.status == PendingStatus.PENDING)
            .values(status=status, decided_by=name, decided_at=_now(), decision_note=note)
            .returning(PendingAction.id)
        ).first()
        session.commit()
        return claimed is not None


def load(factory: sessionmaker[Session], action_id: uuid.UUID) -> PendingAction | None:
    with factory() as session:
        return session.get(PendingAction, action_id)


def _set(factory: sessionmaker[Session], action_id: uuid.UUID, **values: Any) -> None:  # noqa: ANN401
    with factory() as session:
        session.execute(update(PendingAction).where(PendingAction.id == action_id).values(**values))
        session.commit()


def _others_unfinished(factory: sessionmaker[Session], action: PendingAction) -> bool:
    with factory() as session:
        return bool(
            session.scalar(
                select(
                    exists().where(
                        PendingAction.ticket_id == action.ticket_id,
                        PendingAction.id != action.id,
                        PendingAction.status.in_(UNFINISHED),
                    )
                )
            )
        )


async def _db[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:  # noqa: ANN401
    """Accès à la base hors de la boucle d'événements."""
    return await asyncio.to_thread(function, *args, **kwargs)


# --- ShopSim ---------------------------------------------------------------------------


async def recheck(action: PendingAction, *, amounts: bool = True) -> str | None:
    """Motif pour lequel l'action est devenue impossible, ou None. Lève `ShopSimUnavailable`.

    `amounts=False` (relance) : on ne compare pas le reste remboursable, qui a baissé si
    l'action a déjà eu lieu ; ShopSim reconnaît alors la clé et ne refait rien.
    """
    ticket_id, order_id = action.ticket_id, action.params.get("order_id")
    try:
        ticket = await get_json(f"/tickets/{ticket_id}", f"Le ticket {ticket_id} n'existe plus.")
        if ticket["status"] == "resolved":
            return f"Le ticket {ticket_id} a été résolu entre-temps."
        order = await get_json(f"/orders/{order_id}", f"La commande {order_id} n'existe plus.")
        if order["customer_id"] != ticket["customer_id"]:
            return f"La commande {order_id} n'appartient pas au client du ticket {ticket_id}."
        if action.tool == REFUND and amounts:
            payment = await get_json(
                f"/payments/{order_id}", f"Aucun paiement pour la commande {order_id}."
            )
            if payment["status"] != "succeeded":
                return (
                    f"Le paiement de la commande {order_id} n'est plus remboursable "
                    f"(statut : {payment['status']})."
                )
            remaining = payment["amount_cents"] - payment["refunded_cents"]
            requested = action.params["amount_cents"]
            if requested > remaining:
                return (
                    f"Il ne reste que {eur(remaining)} remboursables sur la commande {order_id}, "
                    f"pour {eur(requested)} demandés : un remboursement a eu lieu entre-temps."
                )
    except ShopSimUnavailable:
        raise
    except ShopSimError as exc:
        return str(exc)
    return None


async def _execute(action: PendingAction) -> dict[str, Any]:
    """Même requête que l'outil MCP, avec la clé enregistrée : un rejeu ne refait rien."""
    params, order_id = action.params, action.params["order_id"]
    if action.tool == REFUND:
        payment = await get_json(
            f"/payments/{order_id}", f"Aucun paiement pour la commande {order_id}."
        )
        _, refund = await post_json(
            "/refunds",
            f"Paiement de la commande {order_id} introuvable.",
            {
                "payment_id": payment["id"],
                "amount_cents": params["amount_cents"],
                "reason": params["reason"],
            },
            action.idempotency_key,
        )
        return refund
    if action.tool == RESEND:
        _, resend = await post_json(
            f"/orders/{order_id}/resend",
            f"Commande {order_id} introuvable.",
            {"reason": params["reason"]},
            action.idempotency_key,
        )
        return resend
    raise ValueError(f"outil inconnu : {action.tool}")


async def _run(factory: sessionmaker[Session], action: PendingAction, *, amounts: bool) -> Notice:
    """Revérifie puis exécute une action approuvée ; enregistre le résultat."""
    try:
        impossible = await recheck(action, amounts=amounts)
        if impossible is not None:
            await _db(_failed, factory, action.id, impossible)
            return Notice.IMPOSSIBLE
        result = await _execute(action)
    except ShopSimUnavailable as exc:
        # On ne sait pas si l'action a eu lieu : elle reste approuvée, à relancer
        await _db(_set, factory, action.id, error=str(exc))
        return Notice.UNAVAILABLE
    except ShopSimError as exc:
        # Refus de ShopSim (409, 422, 404) : l'action n'a pas eu lieu
        await _db(_failed, factory, action.id, str(exc))
        return Notice.IMPOSSIBLE
    await _db(
        _set,
        factory,
        action.id,
        status=PendingStatus.EXECUTED,
        executed_at=_now(),
        result=result,
        error=None,
    )
    return Notice.EXECUTED


def _failed(factory: sessionmaker[Session], action_id: uuid.UUID, reason: str) -> None:
    _set(factory, action_id, status=PendingStatus.FAILED, error=reason)


# --- Décisions -------------------------------------------------------------------------


async def approve(
    factory: sessionmaker[Session], action_id: uuid.UUID, name: str, note: str = ""
) -> Notice:
    """Approuve et exécute l'action, une seule fois. Lève `InvalidInput`."""
    decided_by = _text(name, "nom", NAME_MAX, required=True)
    decision_note = _text(note, "motif", NOTE_MAX, required=False)
    assert decided_by is not None
    if not await _db(_claim, factory, action_id, PendingStatus.APPROVED, decided_by, decision_note):
        return Notice.ALREADY_DECIDED
    action = await _db(load, factory, action_id)
    assert action is not None
    return await _run(factory, action, amounts=True)


async def retry(factory: sessionmaker[Session], action_id: uuid.UUID) -> Notice:
    """Relance une action approuvée restée sans résultat (ShopSim en panne), même clé."""
    action = await _db(load, factory, action_id)
    if action is None or action.status is not PendingStatus.APPROVED:
        return Notice.NOT_RETRYABLE
    return await _run(factory, action, amounts=False)


async def reject(
    factory: sessionmaker[Session], action_id: uuid.UUID, name: str, note: str
) -> Notice:
    """Refuse l'action : motif obligatoire, rien n'est envoyé à ShopSim. Lève `InvalidInput`."""
    decided_by = _text(name, "nom", NAME_MAX, required=True)
    decision_note = _text(note, "motif", NOTE_MAX, required=True)
    assert decided_by is not None
    if not await _db(_claim, factory, action_id, PendingStatus.REJECTED, decided_by, decision_note):
        return Notice.ALREADY_DECIDED
    return Notice.REJECTED


async def follow_up(
    factory: sessionmaker[Session],
    action_id: uuid.UUID,
    message: str | None,
    resolve: bool,
) -> tuple[list[Notice], str | None]:
    """Après la décision : message au client, puis résolution du ticket.

    Renvoie les notices et, en cas d'échec, le message de ShopSim. Lève `InvalidInput`.
    """
    text = _text(message, "message", MESSAGE_MAX, required=False) if message else None
    action = await _db(load, factory, action_id)
    if action is None or action.status is PendingStatus.PENDING:
        raise InvalidInput("Décidez d'abord de l'action.")
    notices: list[Notice] = []
    try:
        if text is not None:
            if action.message_sent_at is not None:
                notices.append(Notice.MESSAGE_ALREADY_SENT)
            else:
                await post_json(
                    f"/tickets/{action.ticket_id}/reply",
                    f"Ticket {action.ticket_id} introuvable.",
                    # Écrit par un humain : seule la page d'approbation envoie cet auteur
                    {"body": text, "author": "human"},
                    idempotency.decision_reply_key(action.ticket_id, str(action.id)),
                )
                await _db(_set, factory, action.id, customer_message=text, message_sent_at=_now())
                notices.append(Notice.MESSAGE_SENT)
        if resolve:
            if action.status in UNFINISHED or await _db(_others_unfinished, factory, action):
                notices.append(Notice.RESOLVE_BLOCKED)
            else:
                await post_json(
                    f"/tickets/{action.ticket_id}/resolve",
                    f"Ticket {action.ticket_id} introuvable.",
                )
                await _db(_set, factory, action.id, resolved_at=_now())
                notices.append(Notice.RESOLVED)
    except ShopSimError as exc:
        notices.append(Notice.FOLLOW_UP_FAILED)
        return notices, str(exc)
    return notices, None
