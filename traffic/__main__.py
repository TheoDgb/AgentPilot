"""`python -m traffic` : lance le générateur de trafic.

    python -m traffic                          # 5 req/s, aucun ticket
    python -m traffic --rps 20 --duration 600
    python -m traffic --tickets-per-hour 6 --max-tickets 10

Réglages : options, sinon variables `TRAFFIC_RPS`, `TRAFFIC_TICKETS_PER_HOUR`,
`TRAFFIC_MAX_TICKETS`, `TRAFFIC_DURATION_S`, `TRAFFIC_SEED`. ShopSim : `SHOPSIM_URL`
(`http://localhost:8000` par défaut). Logs JSON sur stderr, et dans `$LOG_DIR/traffic.jsonl` si
`LOG_DIR` est défini : un compte rendu toutes les 30 s (requêtes par route et code, abandons,
tickets créés). Arrêt : Ctrl+C ou SIGTERM (`docker compose stop traffic`).
"""

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
from collections.abc import Sequence

from observability.logs import configure, log_event
from traffic import tickets
from traffic.client import DEFAULT_URL, ShopSimClient, Stats, http_sender
from traffic.pacer import DEFAULT_MAX_IN_FLIGHT, Pacer
from traffic.visits import CatalogHolder, DiscoveryError, discover, requests

logger = logging.getLogger("traffic")

REPORT_INTERVAL_S = 30.0
REFRESH_INTERVAL_S = 600.0
DISCOVERY_RETRY_S = (5.0, 60.0)


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(
        prog="python -m traffic", description="Clients simulés de ShopSim."
    )
    main_parser.add_argument(
        "--rps", type=float, default=_env_float("TRAFFIC_RPS") or 5.0, help="requêtes par seconde"
    )
    main_parser.add_argument(
        "--tickets-per-hour",
        type=float,
        default=_env_float("TRAFFIC_TICKETS_PER_HOUR") or 0.0,
        help=f"nouveaux tickets par heure (0 par défaut, au plus {tickets.MAX_PER_HOUR})",
    )
    main_parser.add_argument(
        "--max-tickets",
        type=int,
        default=int(os.environ["TRAFFIC_MAX_TICKETS"])
        if os.environ.get("TRAFFIC_MAX_TICKETS")
        else None,
        help="nombre total de tickets au plus",
    )
    main_parser.add_argument(
        "--duration",
        type=float,
        default=_env_float("TRAFFIC_DURATION_S"),
        help="durée en secondes (sans limite par défaut)",
    )
    main_parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ["TRAFFIC_SEED"]) if os.environ.get("TRAFFIC_SEED") else None,
        help="graine du tirage des parcours (reproductible)",
    )
    main_parser.add_argument("--max-in-flight", type=int, default=DEFAULT_MAX_IN_FLIGHT)
    return main_parser


def _discover(client: ShopSimClient, stop: threading.Event) -> CatalogHolder | None:
    """Découverte du catalogue, réessayée tant que ShopSim répond mal."""
    delay, ceiling = DISCOVERY_RETRY_S
    while not stop.is_set():
        try:
            catalog = discover(client)
        except DiscoveryError as exc:
            log_event(logger, "traffic.discovery_failed", logging.WARNING, error=str(exc))
            stop.wait(delay)
            delay = min(delay * 2, ceiling)
            continue
        if catalog.customers_with_orders():
            log_event(logger, "traffic.discovered", count=len(catalog.all_orders()))
            return CatalogHolder(catalog)
        log_event(logger, "traffic.discovery_failed", logging.WARNING, error="aucune commande")
        stop.wait(ceiling)
    return None


def _refresh(holder: CatalogHolder, client: ShopSimClient, stop: threading.Event) -> None:
    while not stop.wait(REFRESH_INTERVAL_S):
        try:
            holder.set(discover(client))
        except DiscoveryError as exc:
            # Catalogue précédent gardé : une panne ne coupe pas le trafic
            log_event(logger, "traffic.discovery_failed", logging.WARNING, error=str(exc))


def _report(stats: Stats, rps: float, stop: threading.Event) -> None:
    while not stop.wait(REPORT_INTERVAL_S):
        counts, dropped, created = stats.take()
        log_event(
            logger,
            "traffic.report",
            requests=counts,
            dropped=dropped,
            tickets_created=created,
            rps=rps,
        )


def _tickets(
    args: argparse.Namespace,
    holder: CatalogHolder,
    client: ShopSimClient,
    rng: random.Random,
    stop: threading.Event,
) -> None:
    def create_one() -> bool:
        draft = tickets.draft_ticket(client, holder.get(), rng)
        if draft is None:
            return False
        ticket_id = tickets.create_ticket(client, draft)
        if ticket_id is not None:
            log_event(
                logger,
                "traffic.ticket_created",
                ticket_id=ticket_id,
                order_id=draft.order_id,
                customer_id=draft.customer_id,
                reason=draft.family,
            )
        return ticket_id is not None

    tickets.TicketScheduler(
        args.tickets_per_hour,
        rng,
        max_tickets=args.max_tickets,
        clock=time.monotonic,
        wait=stop.wait,
    ).run(create_one)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    configure("traffic", os.environ.get("LOG_DIR") or None)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    rng = random.Random(args.seed)
    stats = Stats()
    client = ShopSimClient(http_sender(os.environ.get("SHOPSIM_URL") or DEFAULT_URL), stats)
    log_event(logger, "traffic.started", rps=args.rps, count=int(args.tickets_per_hour))

    pacer = Pacer(args.rps, max_in_flight=args.max_in_flight)
    if args.duration is not None:
        # Borne tout, découverte et tickets compris (ShopSim injoignable au démarrage)
        timer = threading.Timer(args.duration, stop.set)
        timer.daemon = True
        timer.start()
    try:
        holder = _discover(client, stop)
        if holder is None:
            return 0
        workers = [
            threading.Thread(target=_refresh, args=(holder, client, stop), daemon=True),
            threading.Thread(target=_report, args=(stats, args.rps, stop), daemon=True),
            threading.Thread(
                target=_tickets,
                args=(args, holder, client, random.Random(rng.random()), stop),
                daemon=True,
            ),
        ]
        for worker in workers:
            worker.start()
        pacer.run(
            requests(holder, client, rng),
            duration_s=args.duration,
            stop=stop,
            on_drop=stats.drop,
        )
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pacer.close(wait=False)
        counts, dropped, created = stats.take()
        log_event(
            logger,
            "traffic.stopped",
            requests=counts,
            dropped=dropped,
            tickets_created=created,
            rps=args.rps,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
