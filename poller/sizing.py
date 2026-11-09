from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable

from poller.orderbook import AskLevel, OrderBook


FeeModel = Callable[[str, Decimal, Decimal], Decimal]
ZERO = Decimal("0")


@dataclass(frozen=True)
class LegEstimate:
    exchange: str
    market: str
    outcome: str
    fills: tuple[AskLevel, ...]
    cost: Decimal
    fees: Decimal


@dataclass(frozen=True)
class TradeEstimate:
    quantity: int
    legs: tuple[LegEstimate, LegEstimate]

    @property
    def gross_profit(self) -> Decimal:
        return Decimal(self.quantity) - sum((leg.cost for leg in self.legs), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((leg.fees for leg in self.legs), ZERO)

    @property
    def estimated_profit(self) -> Decimal:
        return self.gross_profit - self.fees


@dataclass(frozen=True)
class SizingResult:
    pair_id: str
    direction: str
    best_trade: TradeEstimate
    gross_opportunity: TradeEstimate


def _validate(book: OrderBook) -> None:
    previous = ZERO
    for level in book.asks:
        if (not level.price.is_finite() or not level.size.is_finite()
                or not ZERO <= level.price <= 1 or level.size < 0
                or level.price < previous):
            raise ValueError("Expected finite, sorted asks with valid prices and sizes")
        previous = level.price


def _estimate_leg(book: OrderBook, quantity: int, fee_model: FeeModel) -> LegEstimate:
    remaining = Decimal(quantity)
    fills = []
    cost = fees = ZERO
    for level in book.asks:
        if remaining == 0:
            break
        size = min(remaining, level.size)
        if size == 0:
            continue
        fee = fee_model(book.exchange, level.price, size)
        if not isinstance(fee, Decimal) or not fee.is_finite() or fee < 0:
            raise ValueError("Fee model must return finite, nonnegative Decimal dollars")
        fills.append(AskLevel(level.price, size))
        cost += level.price * size
        fees += fee
        remaining -= size
    return LegEstimate(book.exchange, book.market, book.outcome, tuple(fills), cost, fees)


def size_direction(first: OrderBook, second: OrderBook, fee_model: FeeModel,
                   *, max_contracts: int | None = None) -> SizingResult:
    if (first.pair_id != second.pair_id
            or {first.exchange, second.exchange} != {"kalshi", "polymarket"}
            or {first.outcome, second.outcome} != {"YES", "NO"}):
        raise ValueError("Expected opposing outcomes on different exchanges for one pair")
    if max_contracts is not None and (type(max_contracts) is not int or max_contracts < 0):
        raise ValueError("max_contracts must be a nonnegative integer")
    _validate(first)
    _validate(second)
    limit = int(min(sum((x.size for x in first.asks), ZERO),
                    sum((x.size for x in second.asks), ZERO)))
    if max_contracts is not None:
        limit = min(limit, max_contracts)

    def estimate(quantity):
        return TradeEstimate(quantity, (_estimate_leg(first, quantity, fee_model),
                                        _estimate_leg(second, quantity, fee_model)))

    best = gross = estimate(0)
    for quantity in range(1, limit + 1):
        candidate = estimate(quantity)
        if candidate.estimated_profit > best.estimated_profit:
            best = candidate
        if candidate.gross_profit > gross.gross_profit:
            gross = candidate
    direction = f"{first.exchange} {first.outcome} + {second.exchange} {second.outcome}"
    return SizingResult(first.pair_id, direction, best, gross)


def size_books(books: Iterable[OrderBook], fee_model: FeeModel,
               *, max_contracts: int | None = None) -> list[SizingResult]:
    grouped = {}
    for book in books:
        sides = grouped.setdefault(book.pair_id, {})
        key = (book.exchange, book.outcome)
        if key in sides:
            raise ValueError(f"Duplicate book for {book.pair_id}: {key}")
        sides[key] = book
    results = []
    for sides in grouped.values():
        for outcome, opposite in (("YES", "NO"), ("NO", "YES")):
            first = sides.get(("kalshi", outcome))
            second = sides.get(("polymarket", opposite))
            if first is not None and second is not None:
                results.append(size_direction(first, second, fee_model,
                                              max_contracts=max_contracts))
    return results
