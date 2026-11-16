"""Tests for the Kalshi WebSocket feed. Keys are generated per test; a fake local server stands in for Kalshi."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from poller import ws_feed
from poller.livebook import DISCONNECTED, LIVE, RESYNCING
from poller.orderbook import AskLevel
from poller.ws_kalshi import SIGN_PATH, KalshiFeed

AT = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
PAIR = {"id": "fed", "active": True, "kalshi_ticker": "KXFED-26DEC-H25"}


def write_key(tmp_path, kind="ed25519"):
    key = ed25519.Ed25519PrivateKey.generate() if kind == "ed25519" else rsa.generate_private_key(65537, 2048)
    path = tmp_path / f"{kind}.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return key, path


def feed_for(tmp_path, **kwargs):
    key, path = write_key(tmp_path)
    feed = KalshiFeed([PAIR], raw_dir=tmp_path, key_id="test-key", key_path=str(path), env="prod", **kwargs)
    assert feed.ready()
    return feed, key


def snapshot(seq, yes=(), no=(), ticker="KXFED-26DEC-H25", sid=1):
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": {
        "market_ticker": ticker, "yes_dollars_fp": [list(l) for l in yes], "no_dollars_fp": [list(l) for l in no]}})


def delta(seq, side, price, change, ticker="KXFED-26DEC-H25", sid=1):
    return json.dumps({"type": "orderbook_delta", "sid": sid, "seq": seq, "msg": {
        "market_ticker": ticker, "price_dollars": price, "delta_fp": change, "side": side}})


def live_feed(tmp_path):
    feed, _ = feed_for(tmp_path)
    feed._on_connect(AT)
    feed.handle(snapshot(1, yes=[("0.40", "100"), ("0.41", "80")], no=[("0.58", "50"), ("0.59", "120")]), AT)
    return feed


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_signature_verifies_with_the_public_key(tmp_path, kind):
    key, path = write_key(tmp_path, kind)
    feed = KalshiFeed([PAIR], raw_dir=tmp_path, key_id="test-key", key_path=str(path), env="prod")
    assert feed.ready()
    headers = feed.auth_headers(timestamp_ms=1794866000000)
    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    message = ("1794866000000" + "GET" + SIGN_PATH).encode()
    if kind == "ed25519":
        key.public_key().verify(signature, message)
    else:
        key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


def test_missing_or_bad_credentials_mean_not_ready(tmp_path):
    assert not KalshiFeed([PAIR], raw_dir=tmp_path, key_id="", key_path="", env="prod").ready()
    assert not KalshiFeed([PAIR], raw_dir=tmp_path, key_id="k", key_path=str(tmp_path / "missing.pem"), env="prod").ready()
    (tmp_path / "junk.pem").write_text("not a key")
    assert not KalshiFeed([PAIR], raw_dir=tmp_path, key_id="k", key_path=str(tmp_path / "junk.pem"), env="prod").ready()
    with pytest.raises(ValueError):
        KalshiFeed([PAIR], raw_dir=tmp_path, key_id="k", key_path="x", env="staging")


def test_snapshot_becomes_yes_and_no_asks(tmp_path):
    yes, no = live_feed(tmp_path).order_books()
    # YES asks come from NO bids (1 - price), NO asks from YES bids
    assert (yes.exchange, yes.pair_id, yes.market, yes.outcome) == ("kalshi", "fed", "KXFED-26DEC-H25", "YES")
    assert yes.asks == (AskLevel(D("0.41"), D("120")), AskLevel(D("0.42"), D("50")))
    assert no.asks == (AskLevel(D("0.59"), D("80")), AskLevel(D("0.60"), D("100")))
    assert yes.fetched_at == AT.isoformat()


def test_deltas_add_and_remove_size(tmp_path):
    feed = live_feed(tmp_path)
    feed.handle(delta(2, "yes", "0.41", "-80.00"), AT)
    feed.handle(delta(3, "no", "0.59", "30.00"), AT)
    feed.handle(delta(4, "yes", "0.42", "5"), AT)
    book = feed.books["KXFED-26DEC-H25"]
    assert book.levels("yes") == [["0.40", "100"], ["0.42", "5"]]
    assert book.levels("no") == [["0.58", "50"], ["0.59", "150.00"]]
    assert book.state == LIVE and not feed.resync_needed


def test_sequence_gap_marks_every_book_resyncing(tmp_path):
    feed = live_feed(tmp_path)
    feed.handle(delta(2, "yes", "0.41", "-1"), AT)
    feed.handle(delta(4, "yes", "0.41", "-1"), AT)   # seq 3 missing
    assert feed.resync_needed
    assert {row["state"] for row in feed.status()} == {RESYNCING}
    assert feed.order_books() == []
    feed.handle(delta(5, "yes", "0.41", "-1"), AT)   # ignored while resyncing
    assert feed.books["KXFED-26DEC-H25"].levels("yes")[-1] == ["0.41", "79"]


def test_delta_before_snapshot_is_an_error(tmp_path):
    feed, _ = feed_for(tmp_path)
    feed._on_connect(AT)
    with pytest.raises(ValueError):
        feed.handle(delta(1, "yes", "0.41", "5"), AT)


def test_negative_level_is_an_error(tmp_path):
    feed = live_feed(tmp_path)
    with pytest.raises(ValueError):
        feed.handle(delta(2, "yes", "0.41", "-81"), AT)


def test_status_has_both_outcomes_and_errors_are_only_logged(tmp_path):
    feed = live_feed(tmp_path)
    feed.handle(json.dumps({"type": "error", "id": 1, "msg": {"code": 8, "msg": "Unknown market"}}), AT)
    feed.handle(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}), AT)
    assert [(r["outcome"], r["state"]) for r in feed.status()] == [("YES", LIVE), ("NO", LIVE)]
    feed._on_disconnect()
    assert {r["state"] for r in feed.status()} == {DISCONNECTED}


def test_signed_connect_and_reconnect_after_a_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_feed, "MIN_RESYNC_SECONDS", 0)
    key, path = write_key(tmp_path)
    connections = []

    async def handler(ws):
        headers = ws.request.headers
        key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
                                (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + SIGN_PATH).encode())
        connections.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}))
        if len(connections) == 1:
            await ws.send(snapshot(1, yes=[("0.40", "100")], no=[("0.58", "50")]))
            await ws.send(delta(3, "yes", "0.40", "-1"))   # seq 2 missing
        else:
            await ws.send(snapshot(1, yes=[("0.45", "7")], no=[("0.50", "9")]))
        await ws.wait_closed()

    async def scenario():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = KalshiFeed([PAIR], raw_dir=tmp_path, url=f"ws://127.0.0.1:{port}",
                              key_id="test-key", key_path=str(path), env="prod")
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
    assert len(connections) == 2
    assert connections[0]["params"] == {"channels": ["orderbook_delta"], "market_tickers": ["KXFED-26DEC-H25"]}
    assert feed.resyncs == 1
    assert feed.books["KXFED-26DEC-H25"].levels("yes") == [["0.45", "7"]]   # from the second snapshot
    assert len(list(tmp_path.rglob("kalshi_ws/*/*.jsonl"))) == 1
