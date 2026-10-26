"""Kalshi poller. Owner: Prabu

Week 1 goal: for every active pair, fetch the Kalshi order book and save the raw
response with a timestamp to data/raw/ BEFORE parsing anything.

What you read from pairs.json:  pair["kalshi_ticker"]
Get the pairs with:             from pairs import load_pairs

Public base URL (no key needed for market data):
    https://api.elections.kalshi.com/trade-api/v2

Remember Kalshi only publishes bids. A NO bid at 0.59 is a YES ask at 0.41.
That conversion is week 2 work, so just save the raw response for now.

One request returns both sides:
    {"orderbook_fp": {"yes_dollars": [["0.4100", "80.00"], ...], "no_dollars": [...]}}
Prices and sizes are strings, and each side is sorted worst bid first.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

logger = logging.getLogger(__name__)


def _save_raw(response, pair_id, fetched_at, data_dir, **metadata):
    folder = Path(data_dir) / fetched_at.strftime("%Y-%m-%d")
    filename = fetched_at.strftime("%H%M%S_%f") + "_" + uuid.uuid4().hex + ".json"
    record = {
        "exchange": "kalshi",
        "pair_id": pair_id,
        "fetched_at": fetched_at.isoformat(),
        "request_url": response.url,
        "http_status": response.status_code,
        "raw_body": response.text,
        **metadata,
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / filename).open("w", encoding="utf-8") as file:
            json.dump(record, file, ensure_ascii=False)
    except OSError:
        logger.exception("Could not save Kalshi response for %s", pair_id)
        return False
    return True


def _pair_id(pair):
    if not isinstance(pair, dict):
        logger.error("Kalshi pair must be an object")
        return None
    pair_id = pair.get("id")
    if not isinstance(pair_id, str) or not pair_id.strip():
        logger.error("Kalshi pair has a missing or invalid id")
        return None
    return pair_id


def _valid_side(levels):
    # An empty side can come back as null or be left out entirely
    if levels is None:
        return True
    return isinstance(levels, list) and all(
        isinstance(level, list) and len(level) == 2 for level in levels
    )


def collect_kalshi(pair, data_dir="data/raw/kalshi"):
    pair_id = _pair_id(pair)
    if pair_id is None or not pair.get("active", True):
        return []

    ticker = pair.get("kalshi_ticker")
    if not isinstance(ticker, str) or not ticker.strip() or "/" in ticker:
        logger.error("Missing or invalid Kalshi ticker for %s", pair_id)
        return []

    url = f"{KALSHI_API}/markets/{ticker}/orderbook"
    try:
        response = requests.get(url, timeout=(5, 10))
    except requests.RequestException:
        logger.exception("Kalshi request failed: %s", pair_id)
        return []

    fetched_at = datetime.now(timezone.utc)
    if not _save_raw(response, pair_id, fetched_at, data_dir, ticker=ticker):
        return []
    if response.status_code == 429:
        logger.warning("Kalshi rate limited us on %s; will retry next cycle", pair_id)
        return []
    try:
        response.raise_for_status()
        book = response.json().get("orderbook_fp")
        if (not isinstance(book, dict) or not _valid_side(book.get("yes_dollars")) or not _valid_side(book.get("no_dollars"))):
            raise ValueError("Kalshi returned an unexpected order book")
    except (requests.RequestException, ValueError, AttributeError):
        logger.exception("Invalid Kalshi response: %s", pair_id)
        return []

    return [{"pair_id": pair_id, "ticker": ticker, "fetched_at": fetched_at.isoformat(), "book": book, }]

if __name__ == "__main__":
    from pairs import loader
    logging.basicConfig(level=logging.INFO)

    for pair in loader.load_pairs():
        pair_id = _pair_id(pair)
        if pair_id is None:
            logger.error("Skipping pair: use id and kalshi_ticker fields")
            continue
        books = collect_kalshi(pair)
        print(f"{pair_id}: saved {len(books)} order books")
