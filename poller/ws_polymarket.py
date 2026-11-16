"""Polymarket WebSocket feed. Public market channel, no key needed.

Subscribing sends a full "book" snapshot for every token, then "price_change"
messages as levels change:

    {"event_type": "book", "asset_id": "...", "bids": [{"price": "0.32", "size": "100"}], "asks": [...]}
    {"event_type": "price_change", "price_changes": [
        {"asset_id": "...", "price": "0.33", "size": "50", "side": "SELL", "best_bid": "0.32", "best_ask": "0.33"}]}

A price_change size is the new total at that price (0 removes it). BUY changes
the bids, SELL changes the asks.

There are no sequence numbers, so we check every change against the best bid and
ask Polymarket reports with it. If they disagree, our book has drifted and we
resync by reconnecting, which brings fresh snapshots. We also resync if the
connection goes quiet for too long.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from poller.livebook import DISCONNECTED, LIVE, RESYNCING, LiveBook, RawLog, _decimal
from poller.orderbook import normalize_polymarket

logger = logging.getLogger(__name__)

URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_SECONDS = 5          # Polymarket wants a text "PING" at least every 10 seconds
SILENCE_SECONDS = 30      # nothing at all for this long means the connection is dead
MIN_RESYNC_SECONDS = 10   # don't reconnect more often than this when books drift
MAX_BACKOFF_SECONDS = 60


def _now():
    return datetime.now(timezone.utc)


class PolymarketFeed:
    exchange = "polymarket"

    def __init__(self, pairs, raw_dir="data/raw", url=URL, clock=_now):
        self.url = url
        self.clock = clock
        self.raw = RawLog(raw_dir, "polymarket_ws")
        self.tokens = {}          # token -> (pair_id, outcome)
        self.books = {}           # token -> LiveBook
        self.connected = False
        self.connected_at = None
        self.last_heard = None
        self.resync_needed = False
        self.resyncs = 0
        self.set_pairs(pairs)

    def set_pairs(self, pairs):
        """Track the active pairs' tokens. Returns True if the token set changed."""
        tokens = {}
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("active") is not True:
                continue
            yes, no = pair.get("poly_yes_token"), pair.get("poly_no_token")
            if not all(isinstance(t, str) and t.isdigit() for t in (yes, no)) or yes == no:
                logger.warning("Skipping %s: missing or invalid Polymarket tokens", pair.get("id"))
                continue
            tokens[yes] = (pair["id"], "YES")
            tokens[no] = (pair["id"], "NO")
        changed = set(tokens) != set(self.tokens)
        self.tokens = tokens
        self.books = {t: self.books.get(t) or LiveBook(("bids", "asks")) for t in tokens}
        if changed and self.connected:
            self.resync_needed = True
        return changed

    def subscribe_message(self):
        return {"assets_ids": sorted(self.tokens), "type": "market"}

    # ---------- message handling (no network, unit tested) ----------

    def handle(self, text, at):
        """Apply one WebSocket message to the books."""
        self.last_heard = at
        if text in ("PONG", "PING"):
            return
        data = json.loads(text)
        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            kind = msg.get("event_type")
            if kind == "book":
                self._apply_book(msg, at)
            elif kind == "price_change":
                self._apply_changes(msg, at)

    def _apply_book(self, msg, at):
        book = self.books.get(str(msg.get("asset_id")))
        if book is None:
            return
        book.apply_snapshot({
            "bids": [(level["price"], level["size"]) for level in msg.get("bids") or []],
            "asks": [(level["price"], level["size"]) for level in msg.get("asks") or []],
        }, at)

    def _apply_changes(self, msg, at):
        reported = {}
        for change in msg.get("price_changes") or []:
            token = str(change.get("asset_id"))
            book = self.books.get(token)
            if book is None or book.state != LIVE:
                continue   # not ours, or still waiting for this token's snapshot
            side = {"BUY": "bids", "SELL": "asks"}.get(change.get("side"))
            if side is None:
                raise ValueError(f"Unknown price_change side {change.get('side')!r}")
            book.set_level(side, change["price"], change["size"], at)
            reported[token] = (change.get("best_bid"), change.get("best_ask"))
        # Compare after the whole message is applied, using the last values reported per token
        for token, (best_bid, best_ask) in reported.items():
            book = self.books[token]
            if (self._differs(best_bid, book.best("bids", highest=True))
                    or self._differs(best_ask, book.best("asks", highest=False))):
                pair_id, outcome = self.tokens[token]
                logger.warning("Polymarket book drifted for %s %s; resyncing", pair_id, outcome)
                book.mark(RESYNCING)
                self.resync_needed = True

    @staticmethod
    def _differs(reported, ours):
        if reported in (None, "") or ours is None:
            return False   # nothing to compare against
        return _decimal(reported, "best price") != ours

    # ---------- what the rest of ArbCheck reads ----------

    def order_books(self):
        """Live books in the shared OrderBook format. fetched_at is when we last heard from the feed."""
        if not self.connected or self.last_heard is None:
            return []
        books = []
        for token, book in self.books.items():
            if book.state != LIVE:
                continue
            pair_id, outcome = self.tokens[token]
            asks = [{"price": p, "size": s} for p, s in book.levels("asks")]
            books += normalize_polymarket({"pair_id": pair_id, "outcome": outcome,
                                           "fetched_at": self.last_heard.isoformat(),
                                           "book": {"asset_id": token, "asks": asks}})
        return books

    def status(self):
        return [{"exchange": self.exchange, "pair_id": pair_id, "outcome": outcome, "market": token,
                 "state": self.books[token].state,
                 "updated_at": self.books[token].updated_at.isoformat() if self.books[token].updated_at else None,
                 "last_heard": self.last_heard.isoformat() if self.last_heard else None}
                for token, (pair_id, outcome) in self.tokens.items()]

    # ---------- connection ----------

    def _on_connect(self, at):
        self.connected, self.connected_at, self.last_heard = True, at, at
        self.resync_needed = False
        for book in self.books.values():
            book.mark(RESYNCING)   # until its snapshot arrives

    def _on_disconnect(self):
        self.connected = False
        for book in self.books.values():
            book.mark(DISCONNECTED)

    async def run(self, stop: asyncio.Event):
        """Stay connected until stop is set, reconnecting with backoff."""
        delay = 1
        while not stop.is_set():
            if not self.tokens:
                await _sleep(stop, 5)
                continue
            try:
                async with websockets.connect(self.url, open_timeout=15, max_size=None) as ws:
                    self._on_connect(self.clock())
                    await ws.send(json.dumps(self.subscribe_message()))
                    logger.info("Polymarket feed connected, %d tokens", len(self.tokens))
                    if await self._read(ws, stop):
                        delay = 1
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                logger.warning("Polymarket feed connection problem: %s", exc)
            finally:
                self._on_disconnect()
            if not stop.is_set():
                await _sleep(stop, delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    async def _read(self, ws, stop):
        """Read until we need to reconnect. Returns True if any message arrived."""
        got_any = False
        last_ping = asyncio.get_running_loop().time()
        while not stop.is_set():
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_ping >= PING_SECONDS:
                await ws.send("PING")
                last_ping = loop_time
            try:
                text = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                text = None
            at = self.clock()
            if text is not None:
                got_any = True
                self.raw.write(text, at)
                try:
                    self.handle(text, at)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Bad Polymarket message, resyncing: %s", exc)
                    self.resync_needed = True
            if (at - self.last_heard).total_seconds() > SILENCE_SECONDS:
                logger.warning("Polymarket feed silent for %ds; reconnecting", SILENCE_SECONDS)
                return got_any
            if self.resync_needed and (at - self.connected_at).total_seconds() >= MIN_RESYNC_SECONDS:
                self.resyncs += 1
                return got_any
        return got_any


async def _sleep(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
