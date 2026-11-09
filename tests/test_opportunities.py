"""Tests for poller/opportunities.py. Every expected number here is worked out by hand.

These tests bring their own stand-in fee so they test the logging, not the fee model.
The real fee model lives in poller/fees.py and has its own tests.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import poller.opportunities as opp
from poller.opportunities import (
    CycleChecker,
    OpportunityTracker,
    books_from_raw,
    finish_log,
    log_cycle,
    observe,
    read_raw_files,
    replay,
)
from poller.orderbook import AskLevel, OrderBook


def stand_in_fee(exchange, price, quantity):
    """Stand-in fee for these tests only: the brief's Kalshi formula, unrounded, $0 on Polymarket."""
    if exchange == "kalshi":
        return D("0.07") * quantity * price * (1 - price)
    return D("0")


_real_fee_model = opp.FEE_MODEL


def setup_module():
    # The default fee model is whatever poller/fees.py provides. Pin the stand-in here.
    opp.FEE_MODEL = stand_in_fee


def teardown_module():
    opp.FEE_MODEL = _real_fee_model

T0 = datetime(2026, 11, 9, 15, 0, 0, tzinfo=timezone.utc)


def book(exchange, outcome, levels, at=T0, pair="fed"):
    asks = tuple(AskLevel(D(p), D(s)) for p, s in levels)
    return OrderBook(exchange, pair, f"{exchange}-{outcome}", outcome, asks, at.isoformat())


# The example trade plan from the project brief
BRIEF_KALSHI_YES = [("0.41", "80"), ("0.42", "150")]
BRIEF_POLY_NO = [("0.55", "120"), ("0.56", "300")]


def brief_books(at=T0):
    return book("kalshi", "YES", BRIEF_KALSHI_YES, at), book("polymarket", "NO", BRIEF_POLY_NO, at)


# ---------- one check ----------

def test_brief_example_is_tradable():
    obs = observe(*brief_books(), at=T0)
    assert obs.gross_edge == D("0.04")
    assert obs.fee == D("0.016933")
    assert obs.net_edge == D("0.023067")
    assert obs.size == 230
    assert OpportunityTracker().result(obs.net_edge) == "tradable"


def test_no_gap_returns_none():
    k = book("kalshi", "YES", [("0.57", "100")])
    p = book("polymarket", "NO", [("0.43", "100")])
    assert observe(k, p, at=T0) is None


def test_fees_wipe_it_out():
    k = book("kalshi", "YES", [("0.63", "300")])
    p = book("polymarket", "NO", [("0.36", "300")])
    obs = observe(k, p, at=T0)
    assert obs.gross_edge == D("0.01")
    assert obs.net_edge < 0
    assert obs.best_size == 0
    assert OpportunityTracker().result(obs.net_edge) == "fees_wiped_it_out"


def test_below_minimum():
    k = book("kalshi", "YES", [("0.54", "85")])
    p = book("polymarket", "NO", [("0.44", "85")])
    obs = observe(k, p, at=T0)
    assert D("0") < obs.net_edge < D("0.01")
    assert OpportunityTracker().result(obs.net_edge) == "below_minimum"


# ---------- tracker lifecycle ----------

DIR = "kalshi YES + polymarket NO"


def test_opens_updates_and_closes():
    tracker = OpportunityTracker()
    t1, t2 = T0 + timedelta(seconds=10), T0 + timedelta(seconds=20)
    assert tracker.update("fed", DIR, T0, observe(*brief_books(T0), at=T0)) == []
    assert tracker.update("fed", DIR, t1, observe(*brief_books(t1), at=t1)) == []
    closed = tracker.update("fed", DIR, t2, None)
    assert len(closed) == 1
    entry = closed[0]
    assert entry["opened_at"] == T0.isoformat()
    assert entry["last_seen_at"] == t1.isoformat()
    assert entry["closed_at"] == t2.isoformat()
    assert entry["end_reason"] == "gap_closed"
    assert entry["checks_seen"] == 2
    assert entry["result"] == "tradable"
    assert tracker.open == {}


def test_stale_price_closes_without_an_end_time():
    tracker = OpportunityTracker()
    tracker.update("fed", DIR, T0, observe(*brief_books(), at=T0))
    closed = tracker.update("fed", DIR, T0 + timedelta(seconds=10), None, stale=True)
    assert closed[0]["end_reason"] == "stale"
    assert closed[0]["closed_at"] is None


def test_finish_marks_still_open():
    tracker = OpportunityTracker()
    tracker.update("fed", DIR, T0, observe(*brief_books(), at=T0))
    entries = tracker.finish()
    assert entries[0]["end_reason"] == "still_open"
    assert entries[0]["closed_at"] is None


# ---------- replay from raw files in the real saved formats ----------

def kalshi_raw(at, no_bids, yes_bids, pair="fed"):
    body = {"orderbook_fp": {"no_dollars": no_bids, "yes_dollars": yes_bids}}
    return {"exchange": "kalshi", "pair_id": pair, "fetched_at": at.isoformat(),
            "http_status": 200, "raw_body": json.dumps(body), "ticker": "KXFED-H25"}


def poly_raw(at, outcome, asks, pair="fed"):
    body = {"asset_id": f"tok-{outcome}", "bids": [], "asks": asks}
    return {"exchange": "polymarket", "pair_id": pair, "fetched_at": at.isoformat(),
            "http_status": 200, "raw_body": json.dumps(body), "outcome": outcome}


def cycle(at, poly_no_asks, poly_offset=0.2):
    p_at = at + timedelta(seconds=poly_offset)
    return [
        # NO bids at 59 and 58 become YES asks at 41 and 42
        kalshi_raw(at, [["0.5800", "150.00"], ["0.5900", "80.00"]], [["0.4000", "100.00"]]),
        poly_raw(p_at, "NO", poly_no_asks),
        poly_raw(p_at, "YES", [{"price": "0.46", "size": "500"}]),
    ]


GAP = [{"price": "0.56", "size": "300"}, {"price": "0.55", "size": "120"}]
NO_GAP = [{"price": "0.60", "size": "300"}]


def test_kalshi_raw_file_becomes_yes_asks():
    books = books_from_raw(kalshi_raw(T0, [["0.5900", "80.00"]], []))
    yes = [b for b in books if b.outcome == "YES"][0]
    assert yes.asks[0].price == D("0.41")
    assert yes.asks[0].size == D("80")


def test_bad_files_are_skipped():
    bad = kalshi_raw(T0, [], [])
    assert books_from_raw({**bad, "http_status": 500}) == []
    assert books_from_raw({**bad, "raw_body": "not json"}) == []


def test_replay_logs_one_gap_from_open_to_close():
    records = (cycle(T0, GAP) + cycle(T0 + timedelta(seconds=10), GAP)
               + cycle(T0 + timedelta(seconds=20), NO_GAP))
    entries = replay(records)
    assert len(entries) == 1
    e = entries[0]
    assert e["direction"] == DIR
    assert e["opened_at"] == (T0 + timedelta(seconds=0.2)).isoformat()
    assert e["closed_at"] == (T0 + timedelta(seconds=20.2)).isoformat()
    assert e["end_reason"] == "gap_closed"
    assert e["checks_seen"] == 2
    assert e["best_prices"] == ["0.4100", "0.5500"]
    assert e["max_size"] == 230
    assert e["result"] == "tradable"


def test_replay_is_the_same_every_time():
    records = cycle(T0, GAP) + cycle(T0 + timedelta(seconds=10), NO_GAP)
    assert replay(list(records)) == replay(list(records))


def test_stale_price_never_opens_an_opportunity():
    # Polymarket books are 8 seconds older than Kalshi's, so the pair is stale
    records = [
        poly_raw(T0, "NO", GAP),
        poly_raw(T0, "YES", [{"price": "0.46", "size": "500"}]),
        kalshi_raw(T0 + timedelta(seconds=8), [["0.5900", "80.00"]], [["0.4000", "100.00"]]),
    ]
    # Put them in one cycle by spacing them under the cycle gap
    records[2]["fetched_at"] = (T0 + timedelta(seconds=2)).isoformat()
    assert replay(records) != []  # fresh enough at 2 seconds
    records[2]["fetched_at"] = (T0 + timedelta(seconds=8)).isoformat()
    assert replay(records) == []  # 8 seconds apart, so never opens


def test_read_raw_files_skips_gamma(tmp_path):
    day = tmp_path / "polymarket" / "2026-11-09"
    (day / "gamma").mkdir(parents=True)
    (day / "a.json").write_text(json.dumps(poly_raw(T0, "NO", GAP)))
    (day / "gamma" / "meta.json").write_text(json.dumps({"pair_id": "fed", "fetched_at": T0.isoformat()}))
    records = read_raw_files(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "NO"


# ---------- live: what poller/run.py calls every cycle ----------


def test_cycle_checker_opens_and_closes_across_cycles():
    checker = CycleChecker()
    assert checker.check_cycle(list(brief_books(T0))) == []
    assert [o.direction for o in checker.just_opened] == [DIR]
    t1 = T0 + timedelta(seconds=10)
    no_gap = [book("kalshi", "YES", [("0.41", "80")], t1), book("polymarket", "NO", [("0.60", "100")], t1)]
    closed = checker.check_cycle(no_gap)
    assert len(closed) == 1
    assert closed[0]["end_reason"] == "gap_closed"
    assert closed[0]["closed_at"] == t1.isoformat()


def test_empty_cycle_closes_open_gaps_as_stale():
    checker = CycleChecker()
    checker.check_cycle(list(brief_books(T0)))
    closed = checker.check_cycle([])  # every request failed this cycle
    assert closed[0]["end_reason"] == "stale"


def test_one_exchange_going_quiet_closes_as_stale():
    checker = CycleChecker()
    checker.check_cycle(list(brief_books(T0)))
    t1 = T0 + timedelta(seconds=10)
    closed = checker.check_cycle([book("kalshi", "YES", BRIEF_KALSHI_YES, t1)])  # no Polymarket this cycle
    assert closed[0]["end_reason"] == "stale"


def test_live_log_appends_closed_and_still_open(tmp_path):
    out = tmp_path / "live.jsonl"
    checker = CycleChecker()
    log_cycle(checker, list(brief_books(T0)), out)
    assert not out.exists()  # nothing has closed yet
    t1 = T0 + timedelta(seconds=10)
    log_cycle(checker, [book("kalshi", "YES", [("0.41", "80")], t1),
                        book("polymarket", "NO", [("0.60", "100")], t1)], out)
    log_cycle(checker, list(brief_books(T0 + timedelta(seconds=20))), out)
    finish_log(checker, out)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["end_reason"] for line in lines] == ["gap_closed", "still_open"]


def test_default_fee_model_comes_from_fees_py():
    import poller.fees
    assert _real_fee_model.__module__ == poller.fees.__name__


def test_live_log_never_raises(tmp_path):
    class Broken(CycleChecker):
        def check_cycle(self, books):
            raise RuntimeError("boom")
    log_cycle(Broken(), [], tmp_path / "live.jsonl")  # must not raise
