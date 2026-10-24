"""Données de démo déterministes : 50 clients, 200 commandes.

Les ids ne sont jamais forcés : les séquences PostgreSQL les génèrent, ce qui
garde les séquences cohérentes pour les INSERT futurs. Ils restent stables d'une
exécution à l'autre car `main()` recrée les tables (séquences remises à zéro) et
l'ordre d'insertion est fixe.
"""

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from shopsim.app.db import Base, SessionLocal, engine
from shopsim.app.models import Customer, Order, OrderStatus

SEED = 42
N_CUSTOMERS = 50
N_ORDERS = 200
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)

FIRST_NAMES = [
    "Alice", "Bruno", "Chloé", "David", "Emma", "Farid", "Gaëlle", "Hugo",
    "Inès", "Julien", "Karima", "Léo", "Manon", "Nicolas", "Océane", "Paul",
]  # fmt: skip
LAST_NAMES = [
    "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit",
    "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel",
]  # fmt: skip


def seed(session: Session) -> None:
    """Insère les données de démo dans la session (sans commit)."""
    rng = random.Random(SEED)

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
        orders.append(
            Order(
                customer=customer,
                status=rng.choice(statuses),
                total_cents=rng.randint(500, 50_000),
                currency="EUR",
                created_at=customer.created_at + timedelta(minutes=rng.randint(0, 200_000)),
            )
        )
    session.add_all(orders)
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
