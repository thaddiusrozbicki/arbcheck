from decimal import Decimal as D

from poller.fees import sample_fee
from poller.orderbook import AskLevel, OrderBook
from poller.sizing import size_books, size_direction


def book(exchange, outcome, levels, pair="pair"):
    return OrderBook(exchange, pair, "market", outcome,
                     tuple(AskLevel(D(p), D(q)) for p, q in levels), "timestamp")


def free(exchange, price, quantity):
    return D(0)


def test_walks_depth_and_stops_before_unprofitable_contracts():
    """Make sure we stop buying when the next contracts would eat into our profit."""
    a = book("kalshi", "YES", [(".4", "2"), (".55", "5")])
    b = book("polymarket", "NO", [(".5", "10")])
    result = size_direction(a, b, sample_fee)
    assert result.best_trade.quantity == 2
    assert result.best_trade.gross_profit == D(".2")
    assert result.best_trade.fees == D(".018")
    assert result.best_trade.estimated_profit == D(".182")


def test_fee_wiped_opportunity_retained():
    """Make sure we keep the opportunity for logging even when fees wipe out the profit."""
    result = size_direction(book("kalshi", "YES", [(".499", "10")]),
                            book("polymarket", "NO", [(".5", "10")]), sample_fee)
    assert result.best_trade.quantity == 0
    assert result.gross_opportunity.quantity == 10
    assert result.gross_opportunity.gross_profit == D(".01")
    assert result.gross_opportunity.estimated_profit < 0


def test_both_directions():
    """Make sure we check Kalshi YES with Poly NO and Kalshi NO with Poly YES."""
    books = [book("kalshi", "YES", [(".4", "5")]),
             book("kalshi", "NO", [(".3", "6")]),
             book("polymarket", "YES", [(".6", "8")]),
             book("polymarket", "NO", [(".5", "9")])]
    results = size_books(books, free)
    assert [r.best_trade.quantity for r in results] == [5, 6]
    assert [r.direction for r in results] == [
        "kalshi YES + polymarket NO", "kalshi NO + polymarket YES"]


def test_empty_book():
    """Make sure we buy nothing when one book has no contracts for sale."""
    result = size_direction(book("kalshi", "YES", []),
                            book("polymarket", "NO", [(".5", "10")]), free)
    assert result.best_trade.quantity == 0


def test_break_even_prices():
    """Make sure we buy nothing when the two prices add up to $1 and leave no profit."""
    result = size_direction(book("kalshi", "YES", [(".5", "10")]),
                            book("polymarket", "NO", [(".5", "10")]), free)
    assert result.best_trade.quantity == 0


def test_cap():
    """Make sure we stay under the contract limit even when buying more could make money."""
    result = size_direction(book("kalshi", "YES", [(".4", "10")]),
                            book("polymarket", "NO", [(".5", "10")]), free,
                            max_contracts=3)
    assert result.best_trade.quantity == 3
