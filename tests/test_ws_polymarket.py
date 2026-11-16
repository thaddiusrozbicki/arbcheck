"""Tests for the Polymarket WebSocket feed. A fake local server stands in for Polymarket."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import websockets

from poller import ws_feed
from poller.livebook import DISCONNECTED, LIVE, RESYNCING
from poller.orderbook import AskLevel
from poller.ws_polymarket import PolymarketFeed

AT = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
PAIR = {"id": "fed", "active": True, "poly_yes_token": "111", "poly_no_token": "222"}


def book_msg(token, bids=(), asks=()):
    return {"event_type": "book", "asset_id": token,
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks]}


def change_msg(*changes):
    return {"event_type": "price_change", "price_changes": [
        dict(zip(("asset_id", "price", "size", "side", "best_bid", "best_ask"), c)) for c in changes]}


def live_feed(tmp_path):
    feed = PolymarketFeed([PAIR], raw_dir=tmp_path)
    feed._on_connect(AT)
    feed.handle(json.dumps([book_msg("111", bids=[("0.40", "10")], asks=[("0.45", "5"), ("0.47", "8")]),
                            book_msg("222", bids=[("0.53", "4")], asks=[("0.58", "6")])]), AT)
    return feed


def test_only_active_pairs_with_valid_tokens_are_tracked(tmp_path):
    feed = PolymarketFeed([PAIR, dict(PAIR, id="off", active=False, poly_yes_token="3", poly_no_token="4"),
                           dict(PAIR, id="bad", poly_yes_token="x", poly_no_token="5")], raw_dir=tmp_path)
    assert feed.tokens == {"111": ("fed", "YES"), "222": ("fed", "NO")}
    assert feed.subscribe_messages() == [{"assets_ids": ["111", "222"], "type": "market"}]


def test_books_wait_for_their_snapshot(tmp_path):
    feed = PolymarketFeed([PAIR], raw_dir=tmp_path)
    feed._on_connect(AT)
    assert {row["state"] for row in feed.status()} == {RESYNCING}
    feed.handle(json.dumps(change_msg(("111", "0.44", "3", "SELL", "", "0.44"))), AT)
    assert feed.books["111"].levels("asks") == []   # ignored, no snapshot yet
    feed.handle(json.dumps([book_msg("111", asks=[("0.45", "5")])]), AT)
    assert feed.books["111"].state == LIVE and feed.books["222"].state == RESYNCING


def test_snapshot_becomes_shared_order_books(tmp_path):
    books = live_feed(tmp_path).order_books()
    assert [(b.exchange, b.pair_id, b.market, b.outcome) for b in books] == [
        ("polymarket", "fed", "111", "YES"), ("polymarket", "fed", "222", "NO")]
    assert books[0].asks == (AskLevel(D("0.45"), D("5")), AskLevel(D("0.47"), D("8")))
    assert books[0].fetched_at == AT.isoformat()


def test_price_changes_update_levels_and_match_reported_best(tmp_path):
    feed = live_feed(tmp_path)
    later = AT + timedelta(seconds=2)
    feed.handle(json.dumps(change_msg(("111", "0.44", "3", "SELL", "0.40", "0.44"),
                                      ("111", "0.47", "0", "SELL", "0.40", "0.44"),
                                      ("111", "0.41", "2", "BUY", "0.41", "0.44"))), later)
    book = feed.books["111"]
    assert book.levels("asks") == [["0.44", "3"], ["0.45", "5"]]
    assert book.levels("bids") == [["0.40", "10"], ["0.41", "2"]]
    assert book.state == LIVE and not feed.resync_needed
    assert feed.order_books()[0].fetched_at == later.isoformat()


def test_drift_from_reported_best_triggers_resync(tmp_path):
    feed = live_feed(tmp_path)
    feed.handle(json.dumps(change_msg(("222", "0.59", "1", "SELL", "0.53", "0.57"))), AT)
    assert feed.books["222"].state == RESYNCING
    assert feed.resync_needed
    assert [b.outcome for b in feed.order_books()] == ["YES"]   # the drifted book is left out


def test_unknown_tokens_and_other_events_are_ignored(tmp_path):
    feed = live_feed(tmp_path)
    feed.handle(json.dumps(change_msg(("999", "0.10", "1", "BUY", "0.10", "0.20"))), AT)
    feed.handle(json.dumps({"event_type": "last_trade_price", "asset_id": "111", "price": "0.45"}), AT)
    feed.handle("PONG", AT)
    assert not feed.resync_needed and len(feed.order_books()) == 2


def test_disconnect_hides_all_books(tmp_path):
    feed = live_feed(tmp_path)
    feed._on_disconnect()
    assert feed.order_books() == []
    assert {row["state"] for row in feed.status()} == {DISCONNECTED}


def test_changing_pairs_while_connected_forces_resubscribe(tmp_path):
    feed = live_feed(tmp_path)
    assert not feed.set_pairs([PAIR])
    assert feed.set_pairs([PAIR, dict(PAIR, id="new", poly_yes_token="333", poly_no_token="444")])
    assert feed.resync_needed
    assert feed.books["111"].state == LIVE   # existing books are kept


def test_reconnects_after_drift_and_saves_raw_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_feed, "MIN_RESYNC_SECONDS", 0)
    connections = []

    async def handler(ws):
        connections.append(json.loads(await ws.recv()))
        if len(connections) == 1:
            await ws.send(json.dumps([book_msg("111", asks=[("0.45", "5")]), book_msg("222", asks=[("0.58", "6")])]))
            await ws.send(json.dumps(change_msg(("111", "0.46", "1", "SELL", "", "0.40"))))   # drifted
        else:
            await ws.send(json.dumps([book_msg("111", asks=[("0.50", "2")]), book_msg("222", asks=[("0.52", "6")])]))
        await ws.wait_closed()

    async def scenario():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = PolymarketFeed([PAIR], raw_dir=tmp_path, url=f"ws://127.0.0.1:{port}")
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if len(connections) == 2 and len(feed.order_books()) == 2:
                    break
            stop.set()
            await asyncio.wait_for(task, timeout=5)
            return feed

    feed = asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert len(connections) == 2 and connections[0]["assets_ids"] == ["111", "222"]
    assert feed.resyncs == 1
    assert feed.books["111"].levels("asks") == [["0.50", "2"]]   # from the second snapshot
    assert {row["state"] for row in feed.status()} == {DISCONNECTED}   # stopped cleanly
    saved = [json.loads(line) for path in sorted(tmp_path.rglob("*.jsonl")) for line in path.read_text().splitlines()]
    assert len(saved) == 3
