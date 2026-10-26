"""Clés d'idempotence des actions : calculées par le serveur MCP, jamais par le modèle.

Une clé ne dépend que du ticket et de l'action : un agent qui réessaie la même action
(coupure réseau, nouvelle tentative) envoie la même clé, et ShopSim reconnaît le rejeu.
"""

PREFIX = "agentpilot"


def reply_key(ticket_id: int, customer_messages: int) -> str:
    """Une réponse par message du client : la réponse de l'agent ne change pas la clé,
    un nouveau message du client en ouvre une nouvelle."""
    return f"{PREFIX}:ticket-{ticket_id}:reply:after-{customer_messages}"


def refund_key(ticket_id: int, order_id: int) -> str:
    return f"{PREFIX}:ticket-{ticket_id}:refund:order-{order_id}"


def resend_key(ticket_id: int, order_id: int) -> str:
    return f"{PREFIX}:ticket-{ticket_id}:resend:order-{order_id}"
