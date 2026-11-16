"""Tests for poller/db.py, the database the poller writes and the website reads."""

import json
from decimal import Decimal as D

from poller import db
from poller.orderbook import AskLevel, OrderBook


def book(outcome, price, at, exchange="kalshi", pair="fed"):
    return OrderBook(exchange, pair, "MKT", outcome, (AskLevel(D(price), D("100")),), at)


def entry(end_reason="open", closed_at=None, result="tradable", opened="2026-11-14T15:00:00+00:00"):
    return {"pair_id": "fed", "direction": "kalshi YES + polymarket NO", "opened_at": opened,
            "last_seen_at": opened, "closed_at": closed_at, "end_reason": end_reason, "result": result,
            "best_net_edge": "0.0200", "max_size": 100, "best_trade_profit": "2.0000"}


def test_connect_creates_tables(tmp_path):
    con = db.connect(tmp_path / "a.db")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"latest_books", "opportunities"} <= names
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_newer_book_replaces_older_but_not_the_reverse(tmp_path):
    con = db.connect(tmp_path / "a.db")
    db.save_books(con, [book("YES", "0.41", "2026-11-14T15:00:10+00:00")])
    db.save_books(con, [book("YES", "0.45", "2026-11-14T15:00:00+00:00")])  # older, ignored
    row = con.execute("SELECT asks, fetched_at FROM latest_books").fetchone()
    assert json.loads(row["asks"]) == [["0.41", "100"]]
    db.save_books(con, [book("YES", "0.43", "2026-11-14T15:00:20+00:00")])  # newer, replaces
    assert json.loads(con.execute("SELECT asks FROM latest_books").fetchone()["asks"]) == [["0.43", "100"]]


def test_open_gap_is_updated_in_place_when_it_closes(tmp_path):
    con = db.connect(tmp_path / "a.db")
    db.save_opportunities(con, [entry("open")])
    db.save_opportunities(con, [entry("gap_closed", closed_at="2026-11-14T15:00:30+00:00")])
    rows = con.execute("SELECT end_reason, closed_at FROM opportunities").fetchall()
    assert [(r["end_reason"], r["closed_at"]) for r in rows] == [("gap_closed", "2026-11-14T15:00:30+00:00")]


def test_leftover_open_gaps_become_stale_at_startup(tmp_path):
    con = db.connect(tmp_path / "a.db")
    db.save_opportunities(con, [entry("open")])
    assert db.mark_open_as_stale(con) == 1
    row = con.execute("SELECT end_reason, data FROM opportunities").fetchone()
    assert row["end_reason"] == "stale"
    assert json.loads(row["data"])["end_reason"] == "stale"


def test_backfill_never_overwrites_live_rows(tmp_path):
    con = db.connect(tmp_path / "a.db")
    db.save_opportunities(con, [entry("gap_closed", closed_at="2026-11-14T15:00:30+00:00")])
    db.save_opportunities(con, [entry("still_open")], overwrite=False)
    assert con.execute("SELECT end_reason FROM opportunities").fetchone()[0] == "gap_closed"


def test_save_cycle_never_raises(tmp_path):
    con = db.connect(tmp_path / "a.db")
    db.save_cycle(con, [], [{"pair_id": "broken"}], [])  # missing fields, must only log
    assert con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
