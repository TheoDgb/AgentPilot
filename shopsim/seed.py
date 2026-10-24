"""Données de démo déterministes : 50 clients, 200 commandes, paiements et livraisons.

Les ids ne sont jamais forcés : les séquences PostgreSQL les génèrent, ce qui
garde les séquences cohérentes pour les INSERT futurs. Ils restent stables d'une
exécution à l'autre car `main()` recrée les tables (séquences remises à zéro) et
l'ordre d'insertion est fixe.

Toutes les dates sont calculées à partir de `SIMULATION_NOW` (jamais `now()`),
et sont cohérentes avec le statut de la commande.
"""

import random
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.orm import Session

from shopsim.app.clock import SIMULATION_NOW, simulation_today
from shopsim.app.db import Base, SessionLocal, engine
from shopsim.app.models import (
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
)

SEED = 42
N_CUSTOMERS = 50
N_ORDERS = 200
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)
DELAYED_SHARE = 0.15
N_LOST = 2

FIRST_NAMES = [
    "Alice", "Bruno", "Chloé", "David", "Emma", "Farid", "Gaëlle", "Hugo",
    "Inès", "Julien", "Karima", "Léo", "Manon", "Nicolas", "Océane", "Paul",
]  # fmt: skip
LAST_NAMES = [
    "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit",
    "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel",
]  # fmt: skip
REFUND_REASONS = [
    "Article défectueux", "Colis jamais reçu", "Erreur de taille",
    "Rétractation du client", "Article non conforme",
]  # fmt: skip
# Transporteurs fictifs et préfixe de leurs numéros de suivi
CARRIERS = {"Rapidis": "RPD", "ExpressPost": "EXP", "Colifast": "CLF"}
WAREHOUSE = "Entrepôt Lumen, Orléans"
HUBS = [
    "Plateforme de Paris-Gonesse", "Plateforme de Lyon-Corbas", "Plateforme de Lille-Lesquin",
    "Plateforme de Bordeaux-Mérignac", "Plateforme de Marseille-Vitrolles",
    "Plateforme de Toulouse-Blagnac",
]  # fmt: skip
CITIES = [
    "Paris", "Lyon", "Marseille", "Toulouse", "Nantes", "Lille", "Rennes",
    "Strasbourg", "Bordeaux", "Nice",
]  # fmt: skip

# Statut du paiement cohérent avec celui de la commande
PAYMENT_STATUS_BY_ORDER_STATUS = {
    OrderStatus.PENDING: PaymentStatus.PENDING,
    OrderStatus.PAID: PaymentStatus.SUCCEEDED,
    OrderStatus.SHIPPED: PaymentStatus.SUCCEEDED,
    OrderStatus.DELIVERED: PaymentStatus.SUCCEEDED,
    OrderStatus.CANCELLED: PaymentStatus.FAILED,
    OrderStatus.REFUNDED: PaymentStatus.REFUNDED,
}

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Âge (min, max) d'une commande à SIMULATION_NOW selon son statut
ORDER_AGE_BY_STATUS = {
    OrderStatus.PENDING: (HOUR, 3 * DAY),
    OrderStatus.PAID: (HOUR, 3 * DAY),
    OrderStatus.SHIPPED: (2 * DAY, 12 * DAY),
    OrderStatus.DELIVERED: (5 * DAY, 90 * DAY),
    OrderStatus.CANCELLED: (91 * DAY, 180 * DAY),
    OrderStatus.REFUNDED: (91 * DAY, 180 * DAY),
}
# Âge minimal d'une commande shipped pour que sa date estimée puisse être dépassée
MIN_AGE_DELAYED = 5 * DAY
# Le dernier événement d'un colis perdu date d'au moins 4 jours
LOST_SILENCE = 4 * DAY


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _random_between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    return start + timedelta(minutes=rng.randint(0, _minutes(end - start)))


def _order_created_at(rng: random.Random, status: OrderStatus, customer: Customer) -> datetime:
    min_age, max_age = ORDER_AGE_BY_STATUS[status]
    # Une commande ne précède jamais la création de son client
    earliest = max(SIMULATION_NOW - max_age, customer.created_at)
    latest = SIMULATION_NOW - min_age
    if earliest > latest:
        raise ValueError(f"Client créé trop tard pour une commande {status}")
    return _random_between(rng, earliest, latest)


def _spread(rng: random.Random, start: datetime, end: datetime, n: int) -> list[datetime]:
    """n instants distincts et triés, strictement entre start et end."""
    span = _minutes(end - start)
    if span <= n:
        raise ValueError("Intervalle trop court pour les événements de suivi")
    return [start + timedelta(minutes=m) for m in sorted(rng.sample(range(1, span), n))]


def _shipped_statuses(
    rng: random.Random, shipped: list[Order], n_shipments: int
) -> list[ShipmentStatus]:
    """Statuts des livraisons des commandes shipped, avec des effectifs exacts."""
    n_delayed = round(DELAYED_SHARE * n_shipments)
    by_age = sorted(range(len(shipped)), key=lambda i: shipped[i].created_at)
    lost = set(by_age[:N_LOST])
    candidates = [
        i
        for i in range(len(shipped))
        if i not in lost and SIMULATION_NOW - shipped[i].created_at >= MIN_AGE_DELAYED
    ]
    if len(candidates) < n_delayed:
        raise ValueError(
            f"{len(candidates)} commandes shipped assez anciennes, {n_delayed} requises"
        )
    delayed = set(rng.sample(candidates, n_delayed))

    statuses = []
    for i in range(len(shipped)):
        if i in lost:
            statuses.append(ShipmentStatus.LOST)
        elif i in delayed:
            statuses.append(ShipmentStatus.DELAYED)
        else:
            statuses.append(
                rng.choice([ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY])
            )
    return statuses


def _build_shipment(
    rng: random.Random, order: Order, status: ShipmentStatus, carrier: str, tracking_number: str
) -> Shipment:
    created = order.created_at + timedelta(minutes=rng.randint(12 * 60, 36 * 60))
    city = rng.choice(CITIES)
    today = simulation_today()
    delivered_at = None

    # Dernier événement (instant, lieu, description) et date estimée selon le statut
    final_events: list[tuple[datetime, str, str]]
    if status == ShipmentStatus.DELIVERED:
        estimated = created.date() + timedelta(days=3)
        delivered_at = min(
            created + timedelta(minutes=rng.randint(_minutes(DAY), _minutes(4 * DAY))),
            SIMULATION_NOW - timedelta(minutes=30),
        )
        out_at = delivered_at - timedelta(minutes=rng.randint(60, 300))
        final_events = [
            (out_at, city, "En cours de livraison"),
            (delivered_at, city, "Livré"),
        ]
    elif status == ShipmentStatus.OUT_FOR_DELIVERY:
        estimated = today
        out_at = datetime.combine(today, time(7, 0), UTC) + timedelta(minutes=rng.randint(0, 180))
        final_events = [(out_at, city, "En cours de livraison")]
    elif status == ShipmentStatus.IN_TRANSIT:
        estimated = today + timedelta(days=rng.randint(1, 4))
        last_scan = SIMULATION_NOW - timedelta(minutes=rng.randint(30, 600))
        final_events = [(last_scan, rng.choice(HUBS), "En transit")]
    elif status == ShipmentStatus.DELAYED:
        first_day = created.date() + DAY
        last_day = today - DAY
        estimated = first_day + timedelta(days=rng.randint(0, (last_day - first_day).days))
        delay_at = datetime.combine(estimated, time(18, 0), UTC)
        final_events = [(delay_at, rng.choice(HUBS), "Retard d'acheminement")]
    elif status == ShipmentStatus.LOST:
        estimated = created.date() + timedelta(days=3)
        lost_at = SIMULATION_NOW - LOST_SILENCE - timedelta(minutes=rng.randint(0, 24 * 60))
        final_events = [(lost_at, rng.choice(HUBS), "Colis introuvable, enquête ouverte")]
    else:
        raise ValueError(f"Statut non généré par le seed : {status}")

    # Prise en charge puis 1 ou 2 passages en plateforme, entre l'étiquette et le dernier événement
    hubs = rng.sample(HUBS, rng.randint(1, 2))
    middle_times = _spread(rng, created, final_events[0][0], 1 + len(hubs))
    middle_events = [(middle_times[0], WAREHOUSE, "Pris en charge par le transporteur")] + [
        (at, hub, f"En transit — {hub}") for at, hub in zip(middle_times[1:], hubs, strict=True)
    ]
    timeline = [(created, WAREHOUSE, "Étiquette créée"), *middle_events, *final_events]

    return Shipment(
        order=order,
        carrier=carrier,
        tracking_number=tracking_number,
        status=status,
        estimated_delivery=estimated,
        delivered_at=delivered_at,
        created_at=created,
        events=[
            ShipmentEvent(occurred_at=at, location=location, description=description)
            for at, location, description in timeline
        ],
    )


def seed(session: Session) -> None:
    """Insère les données de démo dans la session (sans commit)."""
    rng = random.Random(SEED)
    # Générateur séparé pour les dates et les livraisons : la suite de `rng` (clients,
    # statuts, montants, motifs de remboursement) reste celle de J1.1/J1.2
    timeline_rng = random.Random(SEED + 1)

    customers: list[Customer] = []
    for i in range(1, N_CUSTOMERS + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        customers.append(
            Customer(
                email=f"{first.lower()}.{last.lower()}.{i}@example.com",
                first_name=first,
                last_name=last,
                created_at=BASE_DATE + timedelta(days=rng.randint(0, 60)),
            )
        )
    session.add_all(customers)
    session.flush()

    statuses = list(OrderStatus)
    orders: list[Order] = []
    for _ in range(N_ORDERS):
        customer = rng.choice(customers)
        status = rng.choice(statuses)
        total_cents = rng.randint(500, 50_000)
        # Ancien tirage de date (J1.1), conservé mais inutilisé pour ne pas décaler la suite de rng
        rng.randint(0, 200_000)
        orders.append(
            Order(
                customer=customer,
                status=status,
                total_cents=total_cents,
                currency="EUR",
                created_at=_order_created_at(timeline_rng, status, customer),
            )
        )
    session.add_all(orders)
    session.flush()

    for order in orders:
        payment = Payment(
            order=order,
            status=PAYMENT_STATUS_BY_ORDER_STATUS[order.status],
            amount_cents=order.total_cents,
            created_at=order.created_at + timedelta(minutes=rng.randint(1, 30)),
        )
        session.add(payment)
        if order.status == OrderStatus.REFUNDED:
            session.add(
                Refund(
                    payment=payment,
                    amount_cents=payment.amount_cents,
                    reason=rng.choice(REFUND_REASONS),
                    idempotency_key=f"seed-refund-{order.id}",
                    created_at=payment.created_at + timedelta(days=rng.randint(1, 20)),
                )
            )
    session.flush()

    # Livraisons : seulement pour les commandes shipped et delivered
    shipped = [o for o in orders if o.status == OrderStatus.SHIPPED]
    n_shipments = len(shipped) + sum(o.status == OrderStatus.DELIVERED for o in orders)
    # Même ordre que `shipped` : on consomme les statuts au fil des commandes
    shipped_statuses = iter(_shipped_statuses(timeline_rng, shipped, n_shipments))
    tracking_numbers: set[str] = set()
    for order in orders:
        if order.status == OrderStatus.SHIPPED:
            status = next(shipped_statuses)
        elif order.status == OrderStatus.DELIVERED:
            status = ShipmentStatus.DELIVERED
        else:
            continue
        carrier = timeline_rng.choice(list(CARRIERS))
        prefix = CARRIERS[carrier]
        tracking_number = f"{prefix}{timeline_rng.randrange(10**10):010d}"
        while tracking_number in tracking_numbers:
            tracking_number = f"{prefix}{timeline_rng.randrange(10**10):010d}"
        tracking_numbers.add(tracking_number)
        session.add(_build_shipment(timeline_rng, order, status, carrier, tracking_number))
    session.flush()


def main() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        seed(session)
        session.commit()
    print(f"Seed OK : {N_CUSTOMERS} clients, {N_ORDERS} commandes")


if __name__ == "__main__":
    main()
