import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from pairs import load_pairs
from pairs.validate import validate
from poller.fees import ConfiguredFeeModel
from poller.live import LiveCycleChecker
from poller.opportunities import finish_log, log_cycle
from poller.orderbook import AskLevel, OrderBook
from poller.run import poll_once

T0 = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)


@pytest.fixture
def pair():
    pair = copy.deepcopy(load_pairs()[0])
    pair.update(rules_verified=True, fee_config={
        "verified": True, "source": "Test fixture only; not verified market rates",
        "kalshi": {"formula": "quadratic", "rate": "0.07", "rounding": "kalshi", "balance_precision": "0.01"},
        "polymarket": {"formula": "notional", "rate": "0", "rounding": "polymarket"},
    })
    return pair


def books(pair, at=T0):
    return [
        OrderBook("kalshi", pair["id"], pair["kalshi_ticker"], "YES",
                  (AskLevel(D("0.41"), D("80")), AskLevel(D("0.42"), D("40"))), at.isoformat()),
        OrderBook("polymarket", pair["id"], pair["poly_no_token"], "NO",
                  (AskLevel(D("0.55"), D("120")),), at.isoformat()),
    ]


def test_real_pipeline_sizes_live_records_and_logs_auditable_fees(pair, monkeypatch, tmp_path):
    import poller.run as run
    monkeypatch.setattr(run, "load_pairs", lambda: [pair])
    monkeypatch.setitem(run.COLLECTORS, "kalshi", lambda p: [{
        "pair_id": p["id"], "ticker": p["kalshi_ticker"], "fetched_at": T0.isoformat(),
        "book": {"no_dollars": [["0.59", "80"], ["0.58", "40"]], "yes_dollars": []},
    }])
    monkeypatch.setitem(run.COLLECTORS, "polymarket", lambda p: [{
        "pair_id": p["id"], "outcome": "NO", "fetched_at": T0.isoformat(),
        "book": {"asset_id": p["poly_no_token"], "asks": [{"price": "0.55", "size": "120"}]},
    }])
    checker = LiveCycleChecker(clock=lambda: T0)
    out = tmp_path / "opportunities.jsonl"
    log_cycle(checker, poll_once(checker), out)
    assert checker.just_opened[0].best_size == 120
    assert checker.just_opened[0].best_profit == D("2.36")
    finish_log(checker, out)
    entry = json.loads(out.read_text())
    assert entry["best_trade_profit"] == "2.3600"
    assert entry["fee_config"] == pair["fee_config"]
    assert entry["kalshi_rules_url"] == pair["kalshi_rules_url"]


@pytest.mark.parametrize("change", [
    lambda p: p.pop("rules_verified"),
    lambda p: p.update(rules_verified=False),
    lambda p: p.update(rules_verified="true"),
    lambda p: p.update(active=False),
    lambda p: p.pop("fee_config"),
    lambda p: p["fee_config"].update(verified=False),
    lambda p: p["fee_config"].update(source=""),
    lambda p: p["fee_config"].pop("polymarket"),
    lambda p: p["fee_config"]["kalshi"].update(rate="NaN"),
    lambda p: p["fee_config"]["kalshi"].update(round_up_to="0"),
    lambda p: p["fee_config"]["kalshi"].update(formula="unsupported"),
])
def test_ineligible_pairs_never_reach_sizing(pair, change, monkeypatch):
    import poller.opportunities as opp
    def forbidden(*args, **kwargs):
        pytest.fail("Sizing an ineligible pair")
    monkeypatch.setattr(opp, "size_direction", forbidden)
    change(pair)
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    assert checker.check_cycle(books(pair)) == []
    assert not checker.just_opened
    assert pair["id"] in checker.suppressed


@pytest.mark.parametrize("timestamp", [
    (T0 - timedelta(seconds=5.001)).isoformat(),
    (T0 + timedelta(seconds=0.001)).isoformat(),
    "not-a-time", T0.replace(tzinfo=None).isoformat(),
])
def test_bad_or_stale_quotes_never_reach_sizing(pair, timestamp, monkeypatch):
    from dataclasses import replace
    import poller.opportunities as opp
    def forbidden(*args, **kwargs):
        pytest.fail("Sizing a stale pair")
    monkeypatch.setattr(opp, "size_direction", forbidden)
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    current = [replace(book, fetched_at=timestamp) for book in books(pair)]
    assert checker.check_cycle(current) == []
    assert not checker.just_opened


def test_five_second_boundary_and_request_failure(pair):
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    checker.check_cycle(books(pair, T0 - timedelta(seconds=5)))
    assert len(checker.just_opened) == 1
    closed = checker.check_cycle(books(pair)[:1])
    assert closed[0]["end_reason"] == "stale"
    assert not checker.just_opened


def test_revoked_review_closes_existing_gap(pair):
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    checker.check_cycle(books(pair))
    pair["rules_verified"] = False
    checker.configure([pair])
    closed = checker.check_cycle(books(pair))
    assert closed[0]["end_reason"] == "ineligible"
    assert not checker.just_opened


def test_fee_change_closes_old_estimate_and_uses_new_rules(pair):
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    checker.check_cycle(books(pair))
    pair["fee_config"]["kalshi"]["rate"] = "0.20"
    checker.configure([pair])
    closed = checker.check_cycle(books(pair))
    assert closed[0]["end_reason"] == "configuration_changed"
    assert closed[0]["fee_config"]["kalshi"]["rate"] == "0.07"
    assert checker.just_opened[0].best_size == 0 


def test_wrong_token_and_duplicate_books_suppress_pair(pair):
    from dataclasses import replace
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    current = books(pair)
    checker.check_cycle([current[0], replace(current[1], market="wrong-token")])
    assert not checker.just_opened
    checker.check_cycle(current + current[:1])
    assert not checker.just_opened


def test_pairs_use_their_own_fees(pair):
    other = copy.deepcopy(pair)
    other.update(id="other-pair")
    other["fee_config"]["kalshi"]["rate"] = "0.20"
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair, other])
    checker.check_cycle(books(pair) + books(other))
    assert {obs.pair_id: obs.best_size for obs in checker.just_opened} == {pair["id"]: 120, "other-pair": 0}


def test_config_validation_and_no_implicit_model(pair):
    from poller.opportunities import CycleChecker
    assert validate([pair])[0] == []
    with pytest.raises(TypeError):
        CycleChecker()
    with pytest.raises(ValueError):
        ConfiguredFeeModel.from_config({})


def test_unverified_pairs_still_collect_raw_books(pair, monkeypatch):
    import poller.run as run
    pair["rules_verified"] = False
    monkeypatch.setattr(run, "load_pairs", lambda: [pair])
    called = []
    def collect(p):
        called.append(p["id"])
        return []
    for exchange in run.COLLECTORS:
        monkeypatch.setitem(run.COLLECTORS, exchange, collect)
    checker = LiveCycleChecker(clock=lambda: T0)
    assert poll_once(checker) == []
    assert called == [pair["id"], pair["id"]]
    assert not checker.states


def test_pair_load_failure_revokes_existing_estimates(pair, monkeypatch):
    import poller.run as run
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    checker.check_cycle(books(pair))
    def broken():
        raise ValueError("malformed pairs file")
    monkeypatch.setattr(run, "load_pairs", broken)
    assert poll_once(checker) == []
    closed = checker.check_cycle([])
    assert closed[0]["end_reason"] == "ineligible"
    assert not checker.states


def test_duplicate_pair_ids_suppress_estimates(pair):
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair, copy.deepcopy(pair)])
    checker.check_cycle(books(pair))
    assert not checker.just_opened
    assert checker.suppressed[pair["id"]] == "duplicate pair id"


def test_invalid_second_direction_does_not_publish_partial_plan(pair):
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    current = books(pair) + [
        OrderBook("kalshi", pair["id"], pair["kalshi_ticker"], "NO",
                  (AskLevel(D("0.41"), D("10")),), T0.isoformat()),
        OrderBook("polymarket", pair["id"], pair["poly_yes_token"], "YES",
                  (AskLevel(D("0.55"), D("-1")),), T0.isoformat()),
    ]
    assert checker.check_cycle(current) == []
    assert not checker.just_opened
    assert checker.finish() == []


def test_invalid_cycle_closes_existing_plan_without_partial_reopening(pair):
    from dataclasses import replace
    checker = LiveCycleChecker(clock=lambda: T0)
    checker.configure([pair])
    checker.check_cycle(books(pair))
    current = books(pair)
    current[0] = replace(current[0], asks=(AskLevel(D("0.41"), D("-1")),))
    closed = checker.check_cycle(current)
    assert len(closed) == 1
    assert closed[0]["end_reason"] == "stale"
    assert not checker.just_opened
    assert checker.finish() == []
