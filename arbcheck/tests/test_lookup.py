"""Tests for the parts of lookup_pair.py that don't need the internet."""

import json

import pytest

from scripts.lookup_pair import (
    LookupFailed,
    build_stub,
    extract_poly_slug,
    kalshi_series_from_ticker,
    map_poly_tokens,
)
from pairs.validate import validate


def test_slug_from_bare_slug():
    assert extract_poly_slug("some-market") == "some-market"


def test_slug_from_event_url():
    assert extract_poly_slug("https://polymarket.com/event/big-game") == "big-game"


def test_slug_from_event_and_market_url_takes_market():
    url = "https://polymarket.com/event/big-game/big-game-team-a?tid=123"
    assert extract_poly_slug(url) == "big-game-team-a"


def test_yes_no_tokens_from_json_strings():
    market = {"outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]'}
    assert map_poly_tokens(market) == ("111", "222")


def test_yes_no_order_flipped():
    market = {"outcomes": '["No", "Yes"]', "clobTokenIds": '["111", "222"]'}
    assert map_poly_tokens(market) == ("222", "111")


def test_team_outcomes_need_poly_yes():
    market = {"outcomes": '["Chiefs", "Bills"]', "clobTokenIds": '["111", "222"]'}
    with pytest.raises(LookupFailed):
        map_poly_tokens(market)
    assert map_poly_tokens(market, "bills") == ("222", "111")


def test_series_from_ticker():
    assert kalshi_series_from_ticker("KXNFLGAME-26NOV01KCBUF-KC") == "KXNFLGAME"


def test_stub_passes_validator():
    k = {
        "ticker": "KXEXAMPLE-26NOV01-A",
        "title": "Example game winner?",
        "yes_sub_title": "Team A",
        "close_time": "2026-11-02T03:00:00Z",
    }
    p = {
        "slug": "example-game",
        "question": "Team A vs Team B",
        "outcomes": '["Team A", "Team B"]',
        "clobTokenIds": '["111", "222"]',
        "events": [{"slug": "example-game-event"}],
    }
    yes, no = map_poly_tokens(p, "Team A")
    stub = build_stub(k, p, yes, no, category="sports")
    assert stub["resolution_date"] == "2026-11-02"
    assert stub["poly_rules_url"].endswith("/example-game-event")
    errors, _ = validate([stub])
    assert errors == []
