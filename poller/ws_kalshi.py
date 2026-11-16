"""Kalshi WebSocket feed. Needs a read-only API key (see .env.example).

Subscribing to orderbook_delta sends one snapshot per market, then deltas:

    {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {"market_ticker": "...",
        "yes_dollars_fp": [["0.0500", "100.00"], ...], "no_dollars_fp": [...]}}
    {"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {"market_ticker": "...",
        "price_dollars": "0.0500", "delta_fp": "-100.00", "side": "yes"}}

Like the REST book, both sides are bids. A delta is a change in size, not the new
size. seq counts up by one across the whole subscription, so a skipped number
means we missed an update and every book is suspect. When that happens we mark
them resyncing and reconnect, which brings fresh snapshots.

Every connection is signed: the key signs timestamp + "GET" + "/trade-api/ws/v2".
"""
import base64
import json
import logging
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from poller.livebook import LIVE, RESYNCING, LiveBook
from poller.orderbook import normalize_kalshi
from poller.ws_feed import WebSocketFeed, utc_now

logger = logging.getLogger(__name__)

URLS = {
    "prod": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
}
SIGN_PATH = "/trade-api/ws/v2"


def load_key(path):
    key = serialization.load_pem_private_key(Path(path).expanduser().read_bytes(), password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError("Kalshi keys must be RSA or Ed25519")
    return key


def sign(key, message: bytes) -> str:
    if isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        signature = key.sign(message)
    return base64.b64encode(signature).decode()


class KalshiFeed(WebSocketFeed):
    exchange = "kalshi"
    raw_name = "kalshi_ws"

    def __init__(self, pairs, raw_dir="data/raw", url=None, clock=utc_now,
                 key_id=None, key_path=None, env=None):
        env = env or os.environ.get("KALSHI_ENV", "prod")
        if env not in URLS:
            raise ValueError(f"KALSHI_ENV must be one of {sorted(URLS)}, got {env!r}")
        super().__init__(raw_dir, url or URLS[env], clock)
        self.key_id = key_id if key_id is not None else os.environ.get("KALSHI_API_KEY_ID", "").strip()
        self.key_path = key_path if key_path is not None else os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        self._key = None
        self.tickers = {}      # ticker -> pair_id
        self.last_seq = {}     # subscription id -> last seq seen
        self.set_pairs(pairs)

    def set_pairs(self, pairs):
        """Track the active pairs' tickers. Returns True if the ticker set changed."""
        tickers = {}
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("active") is not True:
                continue
            ticker = pair.get("kalshi_ticker")
            if not isinstance(ticker, str) or not ticker.strip():
                logger.warning("Skipping %s: missing Kalshi ticker", pair.get("id"))
                continue
            if ticker in tickers:
                logger.warning("Skipping %s: ticker %s already used by %s", pair.get("id"), ticker, tickers[ticker])
                continue
            tickers[ticker] = pair["id"]
        changed = set(tickers) != set(self.tickers)
        self.tickers = tickers
        self.books = {t: self.books.get(t) or LiveBook(("yes", "no")) for t in tickers}
        if changed and self.connected:
            self.resync_needed = True
        return changed

    # ---------- connection ----------

    def ready(self):
        if self._key is not None:
            return True
        if not self.key_id or not self.key_path:
            logger.error("Kalshi feed needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH; not starting it")
            return False
        try:
            self._key = load_key(self.key_path)
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not load the Kalshi key from %s: %s", self.key_path, exc)
            return False
        return True

    def auth_headers(self, timestamp_ms=None):
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self._key, (ts + "GET" + SIGN_PATH).encode())}

    def connect_kwargs(self):
        return {"additional_headers": self.auth_headers()}

    def subscribe_messages(self):
        return [{"id": 1, "cmd": "subscribe",
                 "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(self.tickers)}}]

    def _on_connect(self, at):
        super()._on_connect(at)
        self.last_seq = {}

    # ---------- message handling (no network, unit tested) ----------

    def handle(self, text, at):
        self.last_heard = at
        msg = json.loads(text)
        kind = msg.get("type")
        if kind in ("orderbook_snapshot", "orderbook_delta"):
            if not self._sequence_ok(msg):
                return
            body = msg["msg"]
            book = self.books.get(body.get("market_ticker"))
            if book is None:
                return
            if kind == "orderbook_snapshot":
                book.apply_snapshot({"yes": body.get("yes_dollars_fp"), "no": body.get("no_dollars_fp")}, at)
            elif book.state == LIVE:
                book.add_to_level(body["side"], body["price_dollars"], body["delta_fp"], at)
            elif not self.resync_needed:
                raise ValueError(f"Delta for {body.get('market_ticker')} before its snapshot")
        elif kind == "error":
            logger.warning("Kalshi feed error: %s", msg.get("msg"))

    def _sequence_ok(self, msg):
        sid, seq = msg["sid"], msg["seq"]
        last = self.last_seq.get(sid)
        self.last_seq[sid] = seq
        if last is None or seq == last + 1:
            return True
        logger.warning("Kalshi sequence gap on subscription %s (%s then %s); resyncing", sid, last, seq)
        for book in self.books.values():
            book.mark(RESYNCING)
        self.resync_needed = True
        return False

    # ---------- what the rest of ArbCheck reads ----------

    def order_books(self):
        """Live books in the shared OrderBook format (YES and NO asks from the bids)."""
        if not self.connected or self.last_heard is None:
            return []
        books = []
        for ticker, book in self.books.items():
            if book.state == LIVE:
                books += normalize_kalshi({"pair_id": self.tickers[ticker], "ticker": ticker,
                                           "fetched_at": self.last_heard.isoformat(),
                                           "book": {"yes_dollars": book.levels("yes"),
                                                    "no_dollars": book.levels("no")}})
        return books

    def status(self):
        return [{"exchange": self.exchange, "pair_id": pair_id, "outcome": outcome, "market": ticker,
                 "state": self.books[ticker].state,
                 "updated_at": self.books[ticker].updated_at.isoformat() if self.books[ticker].updated_at else None,
                 "last_heard": self.last_heard.isoformat() if self.last_heard else None}
                for ticker, pair_id in self.tickers.items() for outcome in ("YES", "NO")]
