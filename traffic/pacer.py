"""Débit réglable en boucle ouverte : la k-ième requête part à `t0 + k / rps`.

Les clients n'attendent pas les autres : une ShopSim lente ne ralentit pas le débit, elle
accumule des requêtes en cours (c'est ce qui rend les pannes de latence visibles). Au plus
`max_in_flight` requêtes en cours ; au-delà, une requête est abandonnée et comptée. En retard
sur le calendrier (machine surchargée), les créneaux manqués sont abandonnés aussi : jamais de
rattrapage en rafale.

Horloge, sommeil et envoi sont injectables : les tests sont déterministes, sans réseau.
"""

import math
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

Task = Callable[[], object]
Submit = Callable[[Task], object]

DEFAULT_MAX_IN_FLIGHT = 50


class Pacer:
    def __init__(
        self,
        rps: float,
        *,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        submit: Submit | None = None,
    ) -> None:
        if rps <= 0:
            raise ValueError("Le débit doit être positif")
        self.interval = 1.0 / rps
        self.clock = clock
        self.sleep = sleep
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._executor: ThreadPoolExecutor | None = None
        if submit is None:
            self._executor = ThreadPoolExecutor(max_in_flight, thread_name_prefix="traffic")
            submit = self._executor.submit
        self._submit = submit
        self.sent = 0
        self.dropped = 0

    def _wrapped(self, task: Task) -> Task:
        def run() -> None:
            try:
                task()
            finally:
                self._slots.release()

        return run

    def run(
        self,
        tasks: Iterator[Task],
        *,
        duration_s: float | None = None,
        stop: threading.Event | None = None,
        on_drop: Callable[[int], None] | None = None,
    ) -> None:
        """Envoie les tâches au rythme demandé jusqu'à `duration_s`, `stop` ou la fin des tâches."""
        start = self.clock()
        slot = 0
        while not (stop is not None and stop.is_set()):
            due = start + slot * self.interval
            if duration_s is not None and due - start >= duration_s:
                break
            now = self.clock()
            if now < due:
                self.sleep(due - now)
                continue
            # Plus d'un créneau de retard : ceux qui sont passés sont abandonnés
            late = math.floor((now - due) / self.interval)
            if late > 0:
                self._drop(late, on_drop)
                slot += late
                continue
            task = next(tasks, None)
            if task is None:
                break
            if self._slots.acquire(blocking=False):
                self._submit(self._wrapped(task))
                self.sent += 1
            else:
                self._drop(1, on_drop)
            slot += 1

    def _drop(self, count: int, on_drop: Callable[[int], None] | None) -> None:
        self.dropped += count
        if on_drop is not None:
            on_drop(count)

    def close(self, wait: bool = True) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
