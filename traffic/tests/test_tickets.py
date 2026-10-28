"""Nouveaux tickets : désactivés par défaut, plafonnés, cohérents avec les données (le client
parle de sa propre commande, texte de la famille qui correspond à son état)."""

import random
import re
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopsim import ticket_templates as tpl
from shopsim.app.models import Ticket, TicketStatus
from traffic import tickets
from traffic.__main__ import parser
from traffic.client import ShopSimClient
from traffic.tests.conftest import Send, Shop
from traffic.tickets import Family, TicketScheduler, choose_template, create_ticket, draft_ticket

# État de la commande (conftest.STATES) → familles possibles, horloge figée au 2026-07-06 :
# les colis en transit sont attendus le 2026-07-08, pas encore en retard
EXPECTED = {
    "pending": {Family.PAYMENT},
    "cancelled": {Family.PAYMENT},
    "paid": {Family.QUESTION},
    "moving": {Family.QUESTION},
    "delayed": {Family.DELAY},
    "lost": {Family.DELAY},
    "delivered": {Family.REFUND, Family.DEFECTIVE},
}
TEMPLATES = {
    Family.DELAY: tpl.DELAY,
    Family.PAYMENT: tpl.PAYMENT_NOT_CONFIRMED,
    Family.REFUND: tpl.REFUND_REQUEST,
    Family.DEFECTIVE: tpl.DEFECTIVE_PRODUCT,
    Family.QUESTION: tpl.QUESTION_ORDER,
}


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOPSIM_FREEZE_CLOCK", "1")


def matches(template: tpl.Template, subject: str, body: str) -> bool:
    """Le texte vient de ce modèle (champs remplacés par n'importe quoi)."""

    def pattern(part: str) -> re.Pattern[str]:
        pieces = re.split(r"\{\w+\}", part)
        return re.compile("(?s)" + ".+?".join(re.escape(p) for p in pieces))

    return bool(pattern(template[0]).fullmatch(subject) and pattern(template[1]).fullmatch(body))


def test_created_tickets_are_coherent_with_the_data(
    send: Send, shop: Shop, client: TestClient, session: Session
) -> None:
    shopsim = ShopSimClient(send)
    rng = random.Random(11)
    created: list[int] = []

    for _ in range(len(shop.states)):
        draft = draft_ticket(shopsim, shop.catalog, rng)
        assert draft is not None
        ticket_id = create_ticket(shopsim, draft)
        assert ticket_id is not None
        created.append(ticket_id)

        order = shop.orders[draft.order_id]
        # Le client parle de sa propre commande, dans la famille qui correspond à son état
        assert draft.customer_id == order.customer_id
        assert draft.family in EXPECTED[shop.states[order.id]], (shop.states[order.id], draft)
        assert any(matches(t, draft.subject, draft.body) for t in TEMPLATES[draft.family])
        assert "{" not in draft.subject + draft.body and "}" not in draft.subject + draft.body
        assert str(order.id) in draft.subject + draft.body
        assert order.customer.first_name in draft.body

    assert shopsim.stats.take()[2] == len(created)
    session.expire_all()
    for ticket_id in created:
        ticket = client.get(f"/tickets/{ticket_id}").json()
        assert ticket["status"] == "open"
        assert ticket["created_at"] == "2026-07-06T12:00:00Z"
        [message] = ticket["messages"]
        assert message["author"] == "customer"
    # Jamais deux tickets ouverts sur une même commande : toutes les commandes y sont passées
    cited = [session.get_one(Ticket, ticket_id).order_id for ticket_id in created]
    assert len(set(cited)) == len(cited) == len(shop.states)
    # Plus rien à citer : pas de nouveau ticket
    assert draft_ticket(shopsim, shop.catalog, rng) is None


def test_orders_already_cited_by_an_open_ticket_are_skipped(
    send: Send, shop: Shop, make_ticket: Callable[..., Ticket]
) -> None:
    taken = list(shop.orders.values())[:-1]
    for order in taken:
        make_ticket(order.customer, order, status=TicketStatus.OPEN)

    draft = draft_ticket(ShopSimClient(send), shop.catalog, random.Random(2))

    assert draft is not None
    assert draft.order_id == list(shop.orders)[-1]


def test_late_shipment_is_a_delay() -> None:
    order = {"id": 5, "status": "shipped"}
    shipment = {"status": "in_transit", "estimated_delivery": "2026-07-01"}

    family, _ = choose_template(order, shipment, date(2026, 7, 6), random.Random(0))
    on_time, _ = choose_template(order, shipment, date(2026, 6, 30), random.Random(0))

    assert (family, on_time) == (Family.DELAY, Family.QUESTION)


def test_every_template_of_every_family_can_be_filled() -> None:
    """Chaque famille est remplie avec les champs qu'elle aura : livraison seulement si la
    commande est expédiée (paiement non confirmé et question : peut-être pas)."""
    order = {"id": 42, "total_cents": 38_509, "created_at": "2026-06-01T10:00:00Z"}
    shipment = {
        "carrier": "Rapidis",
        "tracking_number": "RPD1",
        "estimated_delivery": "2026-06-05",
        "delivered_at": "2026-06-04T15:00:00Z",
    }
    now = datetime(2026, 7, 6, 12, tzinfo=UTC)
    shipped = tickets.context("Alice", order, shipment, now)
    unshipped = tickets.context("Alice", order, None, now)

    assert (shipped["total"], shipped["delivered"]) == ("385,09 €", "04/06/2026")
    assert shipped["days_since_order"] == 35
    for family, templates in TEMPLATES.items():
        fields = unshipped if family in (Family.PAYMENT, Family.QUESTION) else shipped
        for subject, body in templates:
            assert "{" not in (subject + body).format(**fields)


# --- Désactivé par défaut et plafonné -------------------------------------------------------


def test_ticket_creation_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAFFIC_TICKETS_PER_HOUR", raising=False)
    calls: list[int] = []

    def create_one() -> bool:
        calls.append(1)
        return True

    args = parser().parse_args([])
    TicketScheduler(
        args.tickets_per_hour, random.Random(0), clock=lambda: 0.0, wait=lambda s: False
    ).run(create_one)

    assert args.tickets_per_hour == 0
    assert calls == []


class SimClock:
    def __init__(self, end: float) -> None:
        self.now = 0.0
        self.end = end

    def wait(self, seconds: float) -> bool:
        self.now += seconds
        return self.now >= self.end


def test_at_most_sixty_tickets_per_sliding_hour() -> None:
    clock = SimClock(end=3 * 3600)
    created: list[float] = []

    def create_one() -> bool:
        created.append(clock.now)
        return True

    TicketScheduler(1_000, random.Random(5), clock=lambda: clock.now, wait=clock.wait).run(
        create_one
    )

    assert 150 <= len(created) <= 180
    for i, start in enumerate(created):
        in_hour = [t for t in created[i:] if t - start < 3600]
        assert len(in_hour) <= tickets.MAX_PER_HOUR


def test_max_tickets_stops_creation() -> None:
    clock = SimClock(end=10 * 3600)
    created: list[float] = []

    def create_one() -> bool:
        created.append(clock.now)
        return True

    TicketScheduler(
        30, random.Random(5), max_tickets=4, clock=lambda: clock.now, wait=clock.wait
    ).run(create_one)

    assert len(created) == 4


def test_failed_attempts_do_not_count() -> None:
    clock = SimClock(end=3600)
    attempts: list[float] = []

    def failing() -> bool:
        attempts.append(clock.now)
        return False

    scheduler = TicketScheduler(
        60, random.Random(5), max_tickets=2, clock=lambda: clock.now, wait=clock.wait
    )
    scheduler.run(failing)

    assert scheduler.count == 0
    assert len(attempts) > 2
