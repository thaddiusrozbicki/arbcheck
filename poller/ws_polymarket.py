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
import json
import logging

from poller.livebook import LIVE, RESYNCING, LiveBook, _decimal
from poller.orderbook import normalize_polymarket
from poller.ws_feed import WebSocketFeed, utc_now

logger = logging.getLogger(__name__)

URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketFeed(WebSocketFeed):
    exchange = "polymarket"
    raw_name = "polymarket_ws"

    def __init__(self, pairs, raw_dir="data/raw", url=URL, clock=utc_now):
        super().__init__(raw_dir, url, clock)
        self.tokens = {}          # token -> (pair_id, outcome)
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

    def subscribe_messages(self):
        return [{"assets_ids": sorted(self.tokens), "type": "market"}]

    async def keepalive(self, ws):
        await ws.send("PING")   # Polymarket answers with a text "PONG", which handle() counts

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
