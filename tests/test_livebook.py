"""Tests for the in-memory books the WebSocket feeds keep."""

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from poller.livebook import DISCONNECTED, LIVE, RESYNCING, LiveBook, RawLog

AT = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def test_new_book_starts_disconnected_and_empty():
    book = LiveBook(("bids", "asks"))
    assert book.state == DISCONNECTED
    assert book.best("bids", highest=True) is None


def test_snapshot_replaces_everything_and_goes_live():
    book = LiveBook(("bids", "asks"))
    book.apply_snapshot({"bids": [("0.40", "10")], "asks": [("0.45", "5")]}, AT)
    book.apply_snapshot({"bids": [("0.30", "1"), ("0.31", "0")]}, AT)
    assert book.state == LIVE
    assert book.levels("bids") == [["0.30", "1"]]
    assert book.levels("asks") == []


def test_bad_snapshot_leaves_the_old_book_alone():
    book = LiveBook(("bids", "asks"))
    book.apply_snapshot({"bids": [("0.40", "10")]}, AT)
    with pytest.raises(ValueError):
        book.apply_snapshot({"bids": [("0.40", "10"), ("1.5", "1")]}, AT)
    assert book.levels("bids") == [["0.40", "10"]]


def test_set_level_replaces_and_zero_removes():
    book = LiveBook(("asks",))
    book.apply_snapshot({"asks": [("0.45", "5"), ("0.46", "7")]}, AT)
    book.set_level("asks", "0.45", "9", AT)
    book.set_level("asks", "0.46", "0", AT)
    assert book.levels("asks") == [["0.45", "9"]]
    assert book.best("asks", highest=False) == D("0.45")


def test_add_to_level_adds_and_removes_at_zero():
    book = LiveBook(("yes", "no"))
    book.apply_snapshot({"yes": [("0.40", "10")]}, AT)
    book.add_to_level("yes", "0.40", "-4", AT)
    book.add_to_level("yes", "0.41", "3", AT)
    book.add_to_level("yes", "0.41", "-3", AT)
    assert book.levels("yes") == [["0.40", "6"]]


def test_add_below_zero_means_a_missed_update():
    book = LiveBook(("yes",))
    book.apply_snapshot({"yes": [("0.40", "1")]}, AT)
    with pytest.raises(ValueError):
        book.add_to_level("yes", "0.40", "-2", AT)


@pytest.mark.parametrize("side,price,size", [
    ("bids", "NaN", "1"), ("bids", "1.01", "1"), ("bids", "-0.1", "1"),
    ("bids", "0.5", "-1"), ("bids", "0.5", "bad"), ("nope", "0.5", "1"),
])
def test_invalid_levels_are_rejected(side, price, size):
    book = LiveBook(("bids",))
    with pytest.raises(ValueError):
        book.set_level(side, price, size, AT)


def test_best_prices():
    book = LiveBook(("bids", "asks"))
    book.apply_snapshot({"bids": [("0.30", "1"), ("0.32", "1")], "asks": [("0.35", "1"), ("0.34", "1")]}, AT)
    assert book.best("bids", highest=True) == D("0.32")
    assert book.best("asks", highest=False) == D("0.34")
    book.mark(RESYNCING)
    assert book.state == RESYNCING


def test_raw_log_writes_one_line_per_message_in_hourly_files(tmp_path):
    log = RawLog(tmp_path, "polymarket_ws")
    log.write('{"a": 1}', AT)
    log.write("PONG", AT)
    path = tmp_path / "polymarket_ws" / "2026-11-16" / "15.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["raw"] for line in lines] == ['{"a": 1}', "PONG"]
    assert lines[0]["received_at"] == AT.isoformat()
