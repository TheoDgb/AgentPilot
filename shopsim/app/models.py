"""Modèles ShopSim : clients, commandes, paiements, remboursements, renvois, livraisons,
tickets, pannes injectées (Chaos) et ancre de l'horloge de simulation."""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shopsim.app.db import Base


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class ShipmentStatus(StrEnum):
    LABEL_CREATED = "label_created"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELAYED = "delayed"
    LOST = "lost"


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class MessageAuthor(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    HUMAN = "human"


class ChaosScenario(StrEnum):
    PAYMENT_LATENCY = "payment_latency"
    DB_ERRORS = "db_errors"
    CONNECTION_SATURATION = "connection_saturation"
    BAD_DEPLOY = "bad_deploy"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", values_callable=lambda e: [m.value for m in e])
    )
    # Montant en centimes : entier exact, pas de flottant
    total_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship(back_populates="orders")
    payment: Mapped["Payment | None"] = relationship(back_populates="order")
    shipment: Mapped["Shipment | None"] = relationship(back_populates="order")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Un seul paiement par commande
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", values_callable=lambda e: [m.value for m in e])
    )
    amount_cents: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="payment")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment")

    @property
    def refunded_cents(self) -> int:
        return sum(refund.amount_cents for refund in self.refunds)


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"))
    amount_cents: Mapped[int]
    reason: Mapped[str] = mapped_column(String(500))
    # Clé fournie par l'appelant : rejouer la même requête ne crée pas de second remboursement
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    payment: Mapped[Payment] = relationship(back_populates="refunds")


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Une seule livraison par commande
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    carrier: Mapped[str] = mapped_column(String(50))
    tracking_number: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[ShipmentStatus] = mapped_column(
        Enum(ShipmentStatus, name="shipment_status", values_callable=lambda e: [m.value for m in e])
    )
    estimated_delivery: Mapped[date] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="shipment")
    # Historique de suivi, toujours dans l'ordre chronologique
    events: Mapped[list["ShipmentEvent"]] = relationship(
        back_populates="shipment", order_by="ShipmentEvent.occurred_at"
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(255))

    shipment: Mapped[Shipment] = relationship(back_populates="events")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    # Commande citée par le client, facultative (et pas forcément la sienne)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status", values_callable=lambda e: [m.value for m in e]),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Motif interne de l'escalade, jamais montré au client
    escalation_reason: Mapped[str | None] = mapped_column(String(500))

    customer: Mapped[Customer] = relationship()
    order: Mapped[Order | None] = relationship()
    # Fil de discussion, toujours dans l'ordre chronologique
    messages: Mapped[list["TicketMessage"]] = relationship(
        back_populates="ticket",
        # L'id départage deux messages de même date (horloge figée)
        order_by=lambda: [TicketMessage.created_at, TicketMessage.id],
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    author: Mapped[MessageAuthor] = mapped_column(
        Enum(MessageAuthor, name="message_author", values_callable=lambda e: [m.value for m in e])
    )
    # Texte libre écrit par le client : donnée non fiable
    body: Mapped[str] = mapped_column(Text)
    # Clé fournie par l'appelant pour les réponses de l'agent : rejouer la même requête
    # ne crée pas de second message (absente pour les messages du client)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


class SimulationClock(Base):
    """Ancre de l'horloge de simulation : heure réelle du dernier seed (une seule ligne)."""

    __tablename__ = "simulation_clock"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Resend(Base):
    """Renvoi d'un article : une commande de remplacement, avec sa propre livraison."""

    __tablename__ = "resends"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Commande d'origine : un seul renvoi par commande
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    replacement_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    reason: Mapped[str] = mapped_column(String(500))
    # Clé fournie par l'appelant : rejouer la même requête ne crée pas de second renvoi
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(foreign_keys=[order_id])
    replacement_order: Mapped[Order] = relationship(foreign_keys=[replacement_order_id])


def _real_now() -> datetime:
    """Heure réelle par `clock.real_now` (que les tests figent), importé à l'appel : `clock`
    importe ce module."""
    from shopsim.app import clock

    return clock.real_now()


class ChaosFault(Base):
    """Panne active : la ligne existe tant que la panne n'est pas arrêtée.

    Stockée en base pour que tous les processus de l'application la voient.
    """

    __tablename__ = "chaos_faults"

    scenario: Mapped[ChaosScenario] = mapped_column(
        Enum(ChaosScenario, name="chaos_scenario", values_callable=lambda e: [m.value for m in e]),
        primary_key=True,
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Heure réelle de l'activation (pas la date simulée, figée pendant une évaluation) : la
    # panne connection_saturation monte selon le temps réellement écoulé depuis. Par défaut,
    # l'heure réelle de la création : toute panne créée reste valide
    started_real_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_real_now)
