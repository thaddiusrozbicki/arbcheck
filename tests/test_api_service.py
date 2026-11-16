"""Tests for api/service.py, the numbers the website shows. No web server needed."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from api import service
from poller import db
from poller.orderbook import AskLevel, OrderBook

NOW = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc)
PAIR = {"id": "fed", "name": "Fed hikes 25 bps in December", "category": "economics",
        "resolution_date": "2026-12-09", "kalshi_ticker": "KXFED-H25", "poly_slug": "fed-hike",
        "kalshi_rules_url": "https://kalshi.com/markets/kxfed", "poly_rules_url": "https://polymarket.com/event/fed",
        "rule_notes": "Both resolve on the FOMC statement.", "active": True}


PAIR.update(rules_verified=True, fee_config={
    "verified": True, "source": "Test fixture only",
    "kalshi": {"formula": "quadratic", "rate": "0.07", "rounding": "kalshi", "balance_precision": "0.01"},
    "polymarket": {"formula": "notional", "rate": "0", "rounding": "polymarket"},
})


def books_at(at, k_yes=(("0.41", "80"), ("0.42", "150")), p_no=(("0.55", "120"), ("0.56", "300")),
             k_no=(("0.60", "100"),), p_yes=(("0.46", "500"),)):
    def b(exchange, outcome, levels):
        return OrderBook(exchange, "fed", f"{exchange}-{outcome}", outcome,
                         tuple(AskLevel(D(p), D(s)) for p, s in levels), at.isoformat())
    return [b("kalshi", "YES", k_yes), b("polymarket", "NO", p_no),
            b("kalshi", "NO", k_no), b("polymarket", "YES", p_yes)]


def fresh_db(tmp_path, books):
    con = db.connect(tmp_path / "site.db")
    db.save_books(con, books)
    con.commit()
    return con


def test_fresh_gap_gets_a_trade_plan(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW - timedelta(seconds=1)))
    [p] = service.list_pairs(con, pairs=[PAIR], now=NOW)
    assert p["status"] == "trade_plan_ready"
    assert p["best"]["direction"] == "kalshi YES + polymarket NO"
    assert p["best"]["cost"] == 0.96
    assert p["best"]["net_edge"] == 0.02

    d = service.pair_detail(con, "fed", pairs=[PAIR], now=NOW)
    plan = d["trade_plan"]
    assert plan["payout"] == plan["quantity"] > 0
    assert plan["total_cost"] == round(plan["contracts_cost"] + plan["fees"], 2)
    assert d["days_to_resolution"] == 25
    assert len(d["books"]) == 2


def test_stale_price_hides_the_plan(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW - timedelta(seconds=12)))
    d = service.pair_detail(con, "fed", pairs=[PAIR], now=NOW)
    assert d["status"] == "stale"
    assert d["trade_plan"] is None


def test_no_gap(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW, k_yes=(("0.50", "100"),), p_no=(("0.51", "100"),),
                                     k_no=(("0.50", "100"),), p_yes=(("0.51", "100"),)))
    assert service.list_pairs(con, pairs=[PAIR], now=NOW)[0]["status"] == "no_gap"


def test_unverified_pair_gets_no_plan(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW))
    d = service.pair_detail(con, "fed", pairs=[{**PAIR, "rules_verified": False}], now=NOW)
    assert d["status"] == "not_verified"
    assert d["trade_plan"] is None


def test_pair_without_books_is_waiting(tmp_path):
    con = db.connect(tmp_path / "site.db")
    assert service.list_pairs(con, pairs=[PAIR], now=NOW)[0]["status"] == "no_data"
    assert service.pair_detail(con, "nope", pairs=[PAIR], now=NOW) is None


def test_opportunities_and_results(tmp_path):
    con = db.connect(tmp_path / "site.db")
    base = {"pair_id": "fed", "direction": "kalshi YES + polymarket NO", "last_seen_at": None,
            "closed_at": None, "best_gross_edge": "0.0400", "max_size": 100}
    db.save_opportunities(con, [
        {**base, "opened_at": "2026-11-14T14:00:00+00:00", "end_reason": "gap_closed", "result": "fees_wiped_it_out",
         "best_net_edge": "-0.0050", "best_trade_profit": "0.0000"},
        {**base, "opened_at": "2026-11-14T14:30:00+00:00", "end_reason": "gap_closed", "result": "below_minimum",
         "best_net_edge": "0.0050", "best_trade_profit": "0.5000"},
        {**base, "opened_at": "2026-11-14T14:59:00+00:00", "end_reason": "open", "result": "tradable",
         "best_net_edge": "0.0230", "best_trade_profit": "2.3600"},
    ])
    con.commit()
    log = service.list_opportunities(con, pairs=[PAIR])
    assert [e["end_reason"] for e in log] == ["open", "gap_closed", "gap_closed"]
    assert log[0]["name"] == PAIR["name"]
    r = service.results(con)
    assert (r["gaps_before_fees"], r["survived_fees"], r["tradable"], r["open_now"]) == (3, 2, 1, 1)
    assert r["largest_profit"] == 2.36


def test_health_reports_last_poll(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW - timedelta(seconds=3)))
    h = service.health(con, pairs=[PAIR], now=NOW)
    assert h["seconds_since_last_book"] == 3.0
    assert h["active_pairs"] == 1


def test_open_gap_from_a_stopped_poller_shows_as_stale(tmp_path):
    con = db.connect(tmp_path / "site.db")
    db.save_opportunities(con, [{"pair_id": "fed", "direction": "kalshi YES + polymarket NO",
                                 "opened_at": "2026-11-14T14:00:00+00:00", "last_seen_at": "2026-11-14T14:00:10+00:00",
                                 "closed_at": None, "end_reason": "open", "result": "tradable",
                                 "best_gross_edge": "0.04", "best_net_edge": "0.02", "max_size": 10,
                                 "best_trade_profit": "0.20"}])
    con.commit()
    assert service.list_opportunities(con, pairs=[PAIR], now=NOW)[0]["end_reason"] == "stale"
    assert service.results(con, now=NOW)["open_now"] == 0


def test_unreviewed_fees_hide_the_plan(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW))
    pair = {**PAIR, "fee_config": {**PAIR["fee_config"], "verified": False}}
    detail = service.pair_detail(con, "fed", pairs=[pair], now=NOW)
    assert detail["status"] == "not_verified"
    assert detail["trade_plan"] is None
    assert detail["best"]["fee"] is None


def test_future_books_hide_the_plan(tmp_path):
    con = fresh_db(tmp_path, books_at(NOW + timedelta(seconds=1)))
    detail = service.pair_detail(con, "fed", pairs=[PAIR], now=NOW)
    assert detail["status"] == "stale"
    assert detail["trade_plan"] is None
