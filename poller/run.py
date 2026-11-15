#Poll active pairs continuously: python -m poller.run. (Stop with Ctrl+C.)
import argparse
import logging
import math
import time

from pairs import load_pairs
from poller.fees import load_fee_model
from poller.kalshi import collect_kalshi
from poller.polymarket import collect_polymarket
from poller.orderbook import normalize_kalshi, normalize_polymarket
from poller.opportunities import CycleChecker, finish_log, log_cycle

logger = logging.getLogger(__name__)

COLLECTORS = {"kalshi": collect_kalshi, "polymarket": collect_polymarket}
NORMALIZERS = {"kalshi": normalize_kalshi, "polymarket": normalize_polymarket}


def poll_once():
    books = []
    try:
        pairs = load_pairs()
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Could not load pairs; will retry next cycle")
        return books

    if not pairs:
        logger.info("No active pairs to poll")
    for pair in pairs:
        for exchange, collect in COLLECTORS.items():
            try:
                records = collect(pair)
                logger.info("%s %s: saved %d order books",exchange, pair.get("id", "unknown"), len(records),)
                for record in records:
                    try:
                        books.extend(NORMALIZERS[exchange](record))
                    except (ValueError, TypeError, KeyError):
                        logger.exception("Invalid %s book for %s", exchange, pair["id"])
            except Exception:
                logger.exception("%s collection failed for pair %r", exchange, pair)
    return books


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10,
        help="seconds between cycle starts (default: 10); slow cycles do not overlap",
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--no-opportunities", action="store_true",
        help="skip the opportunity log and only save raw data",
    )
    parser.add_argument("--fee-config", help="JSON fee rules for both exchanges")
    args = parser.parse_args(argv)
    fee_model = None
    if not args.no_opportunities:
        if not args.fee_config:
            parser.error("--fee-config is required unless --no-opportunities is set")
        try:
            fee_model = load_fee_model(args.fee_config)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite number greater than zero")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    checker = None if args.no_opportunities else CycleChecker(fee_model=fee_model)
    try:
        while True:
            started = time.monotonic()
            books = poll_once()
            if checker is not None:
                log_cycle(checker, books)
            if args.once:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        logger.info("Poller stopped")
    finally:
        if checker is not None:
            finish_log(checker)


if __name__ == "__main__":
    main()
