"""Débit en boucle ouverte, sur une horloge simulée : aucun réseau, aucun vrai sommeil."""

import itertools
import threading
from collections.abc import Iterator

import pytest

from traffic.pacer import Pacer, Task


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        # Retard ajouté à un sommeil donné (machine surchargée)
        self.oversleep: dict[int, float] = {}
        self._sleeps = itertools.count()

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds + self.oversleep.get(next(self._sleeps), 0.0)


def tasks(clock: FakeClock, sent_at: list[float]) -> Iterator[Task]:
    return itertools.repeat(lambda: sent_at.append(clock.now))


def inline(task: Task) -> None:
    task()


def test_rate_is_respected() -> None:
    clock = FakeClock()
    sent_at: list[float] = []
    pacer = Pacer(20, clock=clock, sleep=clock.sleep, submit=inline)

    pacer.run(tasks(clock, sent_at), duration_s=10)

    assert pacer.sent == 200
    assert pacer.dropped == 0
    gaps = {round(b - a, 9) for a, b in itertools.pairwise(sent_at)}
    assert gaps == {0.05}
    assert sent_at[-1] - sent_at[0] == pytest.approx(9.95)


def test_slow_requests_do_not_slow_the_rate() -> None:
    """Boucle ouverte : les requêtes en cours ne retardent pas les suivantes."""
    clock = FakeClock()
    pending: list[Task] = []
    pacer = Pacer(10, max_in_flight=50, clock=clock, sleep=clock.sleep, submit=pending.append)

    pacer.run(itertools.repeat(lambda: None), duration_s=3)

    assert pacer.sent == 30
    assert len(pending) == 30


def test_saturated_pool_drops_requests() -> None:
    clock = FakeClock()
    pending: list[Task] = []
    drops: list[int] = []
    pacer = Pacer(10, max_in_flight=3, clock=clock, sleep=clock.sleep, submit=pending.append)

    pacer.run(itertools.repeat(lambda: None), duration_s=1, on_drop=drops.append)

    assert (pacer.sent, pacer.dropped, sum(drops)) == (3, 7, 7)
    # Une requête finie libère sa place
    pending[0]()
    pacer.run(itertools.repeat(lambda: None), duration_s=0.1)
    assert pacer.sent == 4


def test_late_slots_are_dropped_not_sent_in_a_burst() -> None:
    clock = FakeClock()
    clock.oversleep = {5: 1.0}
    sent_at: list[float] = []
    pacer = Pacer(10, clock=clock, sleep=clock.sleep, submit=inline)

    pacer.run(tasks(clock, sent_at), duration_s=3)

    # 30 créneaux : ceux de la seconde perdue sont abandonnés, jamais envoyés d'un coup
    assert pacer.sent + pacer.dropped == 30
    assert pacer.dropped >= 9
    assert min(b - a for a, b in itertools.pairwise(sent_at)) >= 0.1 - 1e-9


def test_stop_event_ends_the_run() -> None:
    clock = FakeClock()
    stop = threading.Event()
    sent: list[int] = []

    def task() -> None:
        sent.append(1)
        if len(sent) == 5:
            stop.set()

    pacer = Pacer(100, clock=clock, sleep=clock.sleep, submit=inline)
    pacer.run(itertools.repeat(task), stop=stop)

    assert len(sent) == 5


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Pacer(0)
