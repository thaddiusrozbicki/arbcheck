#Poll active pairs continuously: python -m poller.run. (Stop with Ctrl+C.)
import argparse
import logging
import math
import time

from pairs import load_pairs
from poller.polymarket import collect_polymarket
# import kalshi

logger = logging.getLogger(__name__)

# kalshi collector
COLLECTORS = {"polymarket": collect_polymarket}


def poll_once():
    try:
        pairs = load_pairs()
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Could not load pairs; will retry next cycle")
        return

    if not pairs:
        logger.info("No active pairs to poll")
    for pair in pairs:
        for exchange, collect in COLLECTORS.items():
            try:
                records = collect(pair)
                logger.info("%s %s: saved %d order books",exchange, pair.get("id", "unknown"), len(records),)
            except Exception:
                logger.exception("%s collection failed for pair %r", exchange, pair)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10,
        help="seconds between cycle starts (default: 10); slow cycles do not overlap",
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite number greater than zero")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        while True:
            started = time.monotonic()
            poll_once()
            if args.once:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        logger.info("Poller stopped")


if __name__ == "__main__":
    main()
