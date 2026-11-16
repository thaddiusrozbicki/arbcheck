"""Run the WebSocket feeds: python -m poller.ws_run (stop with Ctrl+C).

Keeps a live order book for every active pair, saves every raw message to
data/raw/<exchange>_ws/, and writes each book's state to the feed_status table
so the website can show it. Runs next to the REST poller, not instead of it.

The Kalshi feed reads its key from the environment. Locally that comes from
.env in the repo root; on the server systemd loads the same file.
"""
import argparse
import asyncio
import logging
import os
import signal
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pairs import load_pairs
from poller import db
from poller.ws_kalshi import KalshiFeed
from poller.ws_polymarket import PolymarketFeed

logger = logging.getLogger(__name__)

STATUS_SECONDS = 5       # how often feed_status is written
PAIRS_SECONDS = 60       # how often pairs.json is re-read
SUMMARY_SECONDS = 60     # how often a one-line summary is logged

FEEDS = {"kalshi": KalshiFeed, "polymarket": PolymarketFeed}


def load_env(path=".env"):
    """Fill os.environ from KEY=VALUE lines, never replacing values that are already set."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _now():
    return datetime.now(timezone.utc)


def _load_pairs():
    try:
        return load_pairs()
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Could not load pairs; keeping the current ones")
        return None


async def supervise(feeds, stop, con):
    """Write status, reload pairs, and log summaries until stop is set."""
    loop = asyncio.get_running_loop()
    last_pairs = last_summary = loop.time()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATUS_SECONDS)
        except asyncio.TimeoutError:
            pass
        now = loop.time()
        if now - last_pairs >= PAIRS_SECONDS:
            last_pairs = now
            pairs = _load_pairs()
            if pairs is not None:
                for feed in feeds:
                    if feed.set_pairs(pairs):
                        logger.info("%s pairs changed; resubscribing", feed.exchange)
        if con is not None:
            checked_at = _now().isoformat()
            for feed in feeds:
                db.save_feed_status(con, feed.exchange, feed.status(), checked_at)
        if now - last_summary >= SUMMARY_SECONDS:
            last_summary = now
            for feed in feeds:
                states = Counter(row["state"] for row in feed.status())
                logger.info("%s feed: %s, %d resyncs so far", feed.exchange, dict(states), feed.resyncs)


async def main_async(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    pairs = _load_pairs() or []
    feeds = [FEEDS[name](pairs, raw_dir=args.raw) for name in args.feeds]
    con = None
    if not args.no_db:
        try:
            con = db.connect()
        except Exception:
            logger.exception("Could not open the database; continuing without it")
    try:
        await asyncio.gather(supervise(feeds, stop, con),
                             *(feed.run(stop) for feed in feeds))
    finally:
        if con is not None:
            checked_at = _now().isoformat()
            for feed in feeds:
                db.save_feed_status(con, feed.exchange, feed.status(), checked_at)
            con.close()
        logger.info("WebSocket feeds stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feeds", nargs="+", choices=sorted(FEEDS), default=sorted(FEEDS),
                        help="which feeds to run (default: all)")
    parser.add_argument("--raw", default="data/raw", help="folder for raw messages")
    parser.add_argument("--no-db", action="store_true", help="don't write feed status to the database")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
