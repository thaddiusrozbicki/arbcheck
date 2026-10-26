"""Polymarket poller. Owner: Steffia"""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
import requests

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com/book"

logger = logging.getLogger(__name__)


def _save_raw(response, pair_id, fetched_at, data_dir, **metadata):
    folder = Path(data_dir) / fetched_at.strftime("%Y-%m-%d")
    if metadata.get("source") == "gamma":
        folder /= "gamma"
    filename = fetched_at.strftime("%H%M%S_%f") + "_" + uuid.uuid4().hex + ".json"
    record = {
        "exchange": "polymarket",
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
        logger.exception("Could not save Polymarket response for %s", pair_id)
        return False
    return True


def _pair_id(pair):
    if not isinstance(pair, dict):
        logger.error("Polymarket pair must be an object")
        return None
    pair_id = pair.get("id")
    if not isinstance(pair_id, str) or not pair_id.strip():
        logger.error("Polymarket pair has a missing or invalid id")
        return None
    return pair_id


def collect_gamma(pair, data_dir="data/raw/polymarket"):
    pair_id = _pair_id(pair)
    if pair_id is None:
        return None
    slug = pair.get("poly_slug")
    if not isinstance(slug, str) or not slug.strip():
        logger.error("Invalid Gamma market slug for %s", pair_id)
        return None
    try:
        response = requests.get(GAMMA_API, params={"slug": slug}, timeout=(5, 10))
    except requests.RequestException:
        logger.exception("Gamma request failed: %s", pair_id)
        return None

    fetched_at = datetime.now(timezone.utc)
    if not _save_raw( response, pair_id, fetched_at, data_dir, source="gamma", market_slug=slug):
        return None
    try:
        response.raise_for_status()
        markets = response.json()
        if not isinstance(markets, list):
            raise ValueError("Gamma returned an unexpected market list")
        matches = [
            market for market in markets
            if isinstance(market, dict) and market.get("slug") == slug
        ]
        if len(matches) != 1:
            raise ValueError("Gamma did not return one matching market")
        market = matches[0]
    except (requests.RequestException, ValueError):
        logger.exception("Invalid Gamma response: %s", pair_id)
        return None

    logger.info("Saved Gamma metadata for %s: %s", pair_id, market.get("question"))
    return market


def collect_polymarket(pair, data_dir="data/raw/polymarket"):
    pair_id = _pair_id(pair)
    if pair_id is None or not pair.get("active", True):
        return []

    market = collect_gamma(pair, data_dir)
    market_id = market.get("id") if market is not None else None
    results = []
    yes_token = pair.get("poly_yes_token")
    no_token = pair.get("poly_no_token")
    if isinstance(yes_token, str) and yes_token == no_token:
        logger.error("YES and NO tokens are identical for %s", pair_id)
        return []

    for outcome, field in (("YES", "poly_yes_token"), ("NO", "poly_no_token")):
        token_id = pair.get(field)
        if not isinstance(token_id, str) or not token_id.isascii() or not token_id.isdigit():
            logger.error("Missing or invalid %s token for %s", outcome, pair_id)
            continue
        try:
            response = requests.get(
                CLOB_API, params={"token_id": token_id}, timeout=(5, 10)
            )
        except requests.RequestException:
            logger.exception("Polymarket request failed: %s %s", pair_id, outcome)
            continue

        fetched_at = datetime.now(timezone.utc)
        if not _save_raw(response, pair_id, fetched_at, data_dir, market_id=market_id, market_slug=pair.get("poly_slug"), outcome=outcome, token_id=token_id,):
            continue
        try:
            response.raise_for_status()
            book = response.json()
            if (not isinstance(book, dict) or str(book.get("asset_id")) != token_id or not isinstance(book.get("bids"), list) or not isinstance(book.get("asks"), list)):
                raise ValueError("CLOB returned an unexpected order book")
        except (requests.RequestException, ValueError):
            logger.exception("Invalid Polymarket response: %s %s", pair_id, outcome)
            continue

        results.append({"pair_id": pair_id, "outcome": outcome, "fetched_at": fetched_at.isoformat(), "book": book, })
    return results

if __name__ == "__main__":
    from pairs import loader
    logging.basicConfig(level=logging.INFO)

    for pair in loader.load_pairs():
        pair_id = _pair_id(pair)
        if pair_id is None:
            logger.error("Skipping pair: use id, poly_yes_token, and poly_no_token fields")
            continue
        books = collect_polymarket(pair)
        print(f"{pair_id}: saved {len(books)} order books")