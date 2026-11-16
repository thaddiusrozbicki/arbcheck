#Poll active pairs continuously: python -m poller.run. (Stop with Ctrl+C.)
import argparse
import logging
import math
import signal
import time

from pairs import load_pairs
from poller import db
from poller.kalshi import collect_kalshi
from poller.polymarket import collect_polymarket
from poller.orderbook import normalize_kalshi, normalize_polymarket
from poller.opportunities import finish_log, log_cycle
from poller.live import LiveCycleChecker

logger = logging.getLogger(__name__)

COLLECTORS = {"kalshi": collect_kalshi, "polymarket": collect_polymarket}
NORMALIZERS = {"kalshi": normalize_kalshi, "polymarket": normalize_polymarket}


def poll_once(checker=None):
    books = []
    try:
        pairs = load_pairs()
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Could not load pairs; will retry next cycle")
        if checker is not None:
            checker.configure([])
        return books

    if checker is not None:
        checker.configure(pairs)
    if not pairs:
        logger.info("No active pairs to poll")
    for pair in pairs:
        for exchange, collect in COLLECTORS.items():
            try:
                records = collect(pair)
                logger.info("%s %s: saved %d order books",exchange, pair.get("id", "unknown"), len(records),)
                # Books from unverified pairs still go to the website so their prices show.
                # LiveCycleChecker keeps them out of sizing until an admin approves the rules.
                for record in records:
                    try:
                        books.extend(NORMALIZERS[exchange](record))
                    except (ValueError, TypeError, KeyError):
                        logger.exception("Invalid %s book for %s", exchange, pair["id"])
            except Exception:
                logger.exception("%s collection failed for pair %r", exchange, pair)
    return books


def open_database():
    """Open the website database. If it fails, the poller still runs and saves raw files."""
    try:
        con = db.connect()
        stale = db.mark_open_as_stale(con)
        if stale:
            logger.info("Marked %d gaps from the last run as stale", stale)
        return con
    except Exception:
        logger.exception("Could not open the database; continuing without it")
        return None


def _stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    # A server stops a service with SIGTERM. Treat it like Ctrl+C so open gaps get saved.
    signal.signal(signal.SIGTERM, _stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10,
        help="seconds between cycle starts (default: 10); slow cycles do not overlap",
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--no-opportunities", action="store_true",
        help="skip the opportunity log and only save raw data",
    )
    parser.add_argument("--no-db", action="store_true",
        help="don't write to the database (the website won't update)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite number greater than zero")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    checker = None if args.no_opportunities else LiveCycleChecker()
    con = None if args.no_db else open_database()
    try:
        while True:
            started = time.monotonic()
            books = poll_once(checker)
            closed = log_cycle(checker, books) if checker is not None else []
            if con is not None:
                open_now = checker.open_entries() if checker is not None else []
                db.save_cycle(con, books, closed, open_now)
            if args.once:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        logger.info("Poller stopped")
    finally:
        if checker is not None:
            still_open = finish_log(checker)
            if con is not None:
                db.save_cycle(con, [], still_open, [])
        if con is not None:
            con.close()


if __name__ == "__main__":
    main()
