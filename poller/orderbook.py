from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal


@dataclass(frozen=True)
class AskLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    exchange: Literal["kalshi", "polymarket"]
    pair_id: str
    market: str
    outcome: Literal["YES", "NO"]
    asks: tuple[AskLevel, ...]
    fetched_at: str


def _level(price, size, *, complement=False):
    try:
        price, size = Decimal(str(price)), Decimal(str(size))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Price and size must be numbers") from exc
    if not price.is_finite() or not size.is_finite():
        raise ValueError("Price and size must be finite")
    if not 0 <= price <= 1 or size < 0:
        raise ValueError("Price must be in [0, 1] and size must be nonnegative")
    return AskLevel(Decimal("1") - price if complement else price, size)


def _sorted(levels):
    return tuple(sorted((level for level in levels if level.size > 0),
                        key=lambda level: level.price))


def normalize_kalshi(record):
    books = []
    for outcome, side in (("YES", "no_dollars"), ("NO", "yes_dollars")):
        asks = _sorted(
            _level(price, size, complement=True)
            for price, size in (record["book"].get(side) or [])
        )
        books.append(OrderBook("kalshi", record["pair_id"], record["ticker"],
                               outcome, asks, record["fetched_at"]))
    return books


def normalize_polymarket(record):
    outcome = record["outcome"]
    if outcome not in ("YES", "NO"):
        raise ValueError("Outcome must be YES or NO")
    book = record["book"]
    asks = _sorted(_level(level["price"], level["size"]) for level in book["asks"])
    return [OrderBook("polymarket", record["pair_id"], str(book["asset_id"]),
                      outcome, asks, record["fetched_at"])]
