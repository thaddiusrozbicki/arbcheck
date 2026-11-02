from decimal import Decimal

import pytest

from poller.orderbook import AskLevel, normalize_kalshi, normalize_polymarket


STAMP = "2026-11-01T12:00:00+00:00"


def kalshi_record(book):
    return dict(pair_id="pair", ticker="KXTEST", fetched_at=STAMP, book=book)


def poly_record(asks, outcome="NO"):
    return dict(pair_id="pair", outcome=outcome, fetched_at=STAMP,
                book={"asset_id": "123", "asks": asks})


def test_kalshi_conversion():
    yes, no = normalize_kalshi(kalshi_record({
        "yes_dollars": [["0.40", "100"], ["0.41", "80"]],
        "no_dollars": [["0.58", "50"], ["0.59", "120"]],
    }))
    assert yes.asks == (AskLevel(Decimal("0.41"), Decimal("120")),
                        AskLevel(Decimal("0.42"), Decimal("50")))
    assert no.asks == (AskLevel(Decimal("0.59"), Decimal("80")),
                       AskLevel(Decimal("0.60"), Decimal("100")))
    assert (yes.exchange, yes.pair_id, yes.market, yes.outcome, yes.fetched_at) == (
        "kalshi", "pair", "KXTEST", "YES", STAMP)
    assert no.outcome == "NO"


def test_empty_kalshi_book():
    yes, no = normalize_kalshi(kalshi_record({"no_dollars": None}))
    assert yes.asks == no.asks == ()


@pytest.mark.parametrize("outcome", ["YES", "NO"])
def test_polymarket_conversion(outcome):
    [book] = normalize_polymarket(poly_record([
        {"price": "0.6", "size": "10"},
        {"price": "0.55", "size": "2.5"},
        {"price": "0.1", "size": "0"},
    ], outcome))
    assert book.asks == (AskLevel(Decimal("0.55"), Decimal("2.5")),
                         AskLevel(Decimal("0.6"), Decimal("10")))
    assert (book.exchange, book.market, book.outcome, book.fetched_at) == (
        "polymarket", "123", outcome, STAMP)
    assert normalize_polymarket(poly_record([]))[0].asks == ()


@pytest.mark.parametrize("price,size", [
    ("NaN", "1"), ("Infinity", "1"), ("0.5", "NaN"),
    ("-0.01", "1"), ("1.01", "1"), ("0.5", "-1"), ("bad", "1"),
])
def test_invalid_prices_and_sizes(price, size):
    with pytest.raises(ValueError):
        normalize_polymarket(poly_record([{"price": price, "size": size}]))
    with pytest.raises(ValueError):
        normalize_kalshi(kalshi_record({"yes_dollars": [[price, size]]}))


def test_poll_once(monkeypatch):
    from poller import run

    monkeypatch.setattr(run, "load_pairs", lambda: [{"id": "pair"}])
    monkeypatch.setattr(run, "COLLECTORS", {
        "kalshi": lambda pair: [kalshi_record({"no_dollars": [["0.59", "80"]]})],
        "polymarket": lambda pair: [poly_record([{"price": "0.55", "size": "120"}])],
    })
    books = run.poll_once()
    assert [(book.exchange, book.outcome) for book in books] == [
        ("kalshi", "YES"), ("kalshi", "NO"), ("polymarket", "NO")]
    assert books[0].asks[0].price + books[2].asks[0].price == Decimal("0.96")
