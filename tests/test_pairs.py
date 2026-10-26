"""Tests for pairs.json and the validator. These run in CI on every PR."""

import json
from datetime import date

from pairs import PAIRS_PATH, load_pairs
from pairs.validate import validate

GOOD = {
    "id": "example-pair",
    "name": "Example market",
    "category": "sports",
    "kalshi_ticker": "KXEXAMPLE-26NOV01-A",
    "poly_slug": "example-market",
    "poly_yes_token": "111",
    "poly_no_token": "222",
    "resolution_date": "2026-12-01",
    "kalshi_rules_url": "https://kalshi.com/markets/kxexample",
    "poly_rules_url": "https://polymarket.com/event/example",
    "rule_notes": "Both resolve on the official league result.",
    "active": True,
}
TODAY = date(2026, 10, 25)


def test_real_pairs_file_is_valid():
    with open(PAIRS_PATH, encoding="utf-8") as f:
        pairs = json.load(f)
    errors, _ = validate(pairs)
    assert errors == [], "\n".join(errors)


def test_good_pair_passes():
    errors, warnings = validate([GOOD], today=TODAY)
    assert errors == [] and warnings == []


def test_missing_field_fails():
    bad = {k: v for k, v in GOOD.items() if k != "poly_no_token"}
    errors, _ = validate([bad], today=TODAY)
    assert any("missing 'poly_no_token'" in e for e in errors)


def test_duplicate_id_fails():
    errors, _ = validate([GOOD, dict(GOOD, kalshi_ticker="KXOTHER-1")], today=TODAY)
    assert any("duplicate id" in e for e in errors)


def test_same_yes_and_no_token_fails():
    errors, _ = validate([dict(GOOD, poly_no_token="111")], today=TODAY)
    assert any("same" in e for e in errors)


def test_bad_date_fails():
    errors, _ = validate([dict(GOOD, resolution_date="12/01/2026")], today=TODAY)
    assert any("YYYY-MM-DD" in e for e in errors)


def test_resolved_but_active_warns():
    _, warnings = validate([dict(GOOD, resolution_date="2026-10-01")], today=TODAY)
    assert any("still active" in w for w in warnings)


def test_empty_rule_notes_warns_not_errors():
    errors, warnings = validate([dict(GOOD, rule_notes="")], today=TODAY)
    assert errors == []
    assert any("rule_notes" in w for w in warnings)


def test_load_pairs_skips_paused(tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps([GOOD, dict(GOOD, id="paused", active=False)]))
    assert [p["id"] for p in load_pairs(path)] == ["example-pair"]
    assert len(load_pairs(path, active_only=False)) == 2
