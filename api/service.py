"""Everything the website shows, worked out from the database.

These are plain functions so they can be tested without a web server. api/main.py
just wraps them in routes. Money math uses the same fee model, minimum edge, and
stale-price rule as the opportunity logger, so the site and the log always agree.
"""

import json
import sqlite3
import statistics
from datetime import date, datetime, timezone
from decimal import Decimal

from pairs import load_pairs
from poller import opportunities as opp
from poller.fees import reviewed_pair_fees
from poller.orderbook import AskLevel, OrderBook
from poller.sizing import size_direction

ONE = Decimal("1")
BOOK_LEVELS_SHOWN = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _f(value: Decimal, places: int = 6) -> float:
    return float(round(value, places))


# ---------- reading books ----------

def load_books(con: sqlite3.Connection) -> dict[tuple[str, str, str], OrderBook]:
    books = {}
    for row in con.execute("SELECT * FROM latest_books"):
        asks = tuple(AskLevel(Decimal(p), Decimal(s)) for p, s in json.loads(row["asks"]))
        books[(row["pair_id"], row["exchange"], row["outcome"])] = OrderBook(
            row["exchange"], row["pair_id"], row["market"] or "", row["outcome"], asks, row["fetched_at"])
    return books


def age_seconds(book: OrderBook, now: datetime) -> float:
    return (now - opp.parse_time(book.fetched_at)).total_seconds()


def book_json(book: OrderBook, now: datetime, levels: int = BOOK_LEVELS_SHOWN) -> dict:
    return {
        "exchange": book.exchange,
        "outcome": book.outcome,
        "market": book.market,
        "fetched_at": book.fetched_at,
        "age_seconds": round(age_seconds(book, now), 1),
        "asks": [[_f(level.price, 4), _f(level.size, 2)] for level in book.asks[:levels]],
    }


# ---------- one direction ----------

def evaluate_direction(first: OrderBook, second: OrderBook, now: datetime, verified: bool = True, *, fee_model=None) -> dict:
    """Best-price check for one direction, using the logger's rules."""
    first_age, second_age = age_seconds(first, now), age_seconds(second, now)
    out = {
        "direction": f"{first.exchange} {first.outcome} + {second.exchange} {second.outcome}",
        "kalshi_side": first.outcome,
        "polymarket_side": second.outcome,
        "kalshi_fetched_at": first.fetched_at,
        "polymarket_fetched_at": second.fetched_at,
        "kalshi_price": None, "polymarket_price": None,
        "cost": None, "gross_edge": None, "fee": None, "net_edge": None,
        "stale": not (opp.is_fresh(first, now) and opp.is_fresh(second, now)),
    }
    if not first.asks or not second.asks:
        out["status"] = "no_data"
        return out
    p1, p2 = first.asks[0].price, second.asks[0].price
    gross = ONE - p1 - p2
    out.update(kalshi_price=_f(p1, 4), polymarket_price=_f(p2, 4), cost=_f(p1 + p2, 4), gross_edge=_f(gross))
    if fee_model is None:
        out["status"] = "not_verified"
        return out
    if gross > 0:
        fee = fee_model(first.exchange, p1, ONE) + fee_model(second.exchange, p2, ONE)
        out.update(fee=_f(fee), net_edge=_f(gross - fee))
    if out["stale"]:
        out["status"] = "stale"
    elif gross <= 0:
        out["status"] = "no_gap"
    elif gross - fee <= 0:
        out["status"] = "fees_wipe_it_out"
    elif gross - fee < opp.MIN_EDGE:
        out["status"] = "below_minimum"
    elif not verified:
        out["status"] = "not_verified"
    else:
        out["status"] = "trade_plan_ready"
    return out


def directions_for(pair_id: str, books: dict) -> list[tuple[OrderBook, OrderBook]]:
    found = []
    for first_key, second_key in opp.DIRECTIONS:
        first = books.get((pair_id, *first_key))
        second = books.get((pair_id, *second_key))
        if first is not None and second is not None:
            found.append((first, second))
    return found


def pair_fee_model(pair):
    try:
        return reviewed_pair_fees(pair)
    except (ValueError, TypeError, KeyError):
        return None


def best_direction(pair: dict, books: dict, now: datetime):
    """The cheaper of the two directions, with its books. None if we don't have both sides yet."""
    options = []
    for first, second in directions_for(pair["id"], books):
        result = evaluate_direction(first, second, now, pair.get("rules_verified") is True, fee_model=pair_fee_model(pair))
        options.append((result, first, second))
    priced = [o for o in options if o[0]["cost"] is not None]
    if priced:
        return min(priced, key=lambda o: o[0]["cost"])
    return options[0] if options else None


# ---------- pages ----------

def pair_meta(pair: dict) -> dict:
    return {
        "id": pair["id"],
        "name": pair.get("name", pair["id"]),
        "category": pair.get("category"),
        "resolution_date": pair.get("resolution_date"),
        "verified": pair_fee_model(pair) is not None,
        "active": pair.get("active", True),
    }


def list_pairs(con: sqlite3.Connection, pairs=None, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    pairs = load_pairs() if pairs is None else pairs
    books = load_books(con)
    out = []
    for pair in pairs:
        best = best_direction(pair, books, now)
        out.append({**pair_meta(pair),
                    "best": best[0] if best else None,
                    "status": best[0]["status"] if best else "no_data"})
    return out


def days_until(resolution_date: str | None, now: datetime) -> int | None:
    if not resolution_date:
        return None
    try:
        return max(1, (date.fromisoformat(resolution_date) - now.date()).days)
    except ValueError:
        return None


def trade_plan(first: OrderBook, second: OrderBook, days: int | None, now: datetime, *, fee_model) -> dict | None:
    """Sizes the trade with the team's sizing engine. None if no size makes money."""
    sizing = size_direction(first, second, fee_model, max_contracts=opp.MAX_CONTRACTS)
    trade = sizing.best_trade
    if trade.quantity < 1:
        return None
    legs = []
    for leg, book in zip(trade.legs, (first, second)):
        legs.append({
            "exchange": leg.exchange,
            "outcome": leg.outcome,
            "fills": [[_f(f.price, 4), _f(f.size, 2)] for f in leg.fills],
            "cost": _f(leg.cost, 2),
            "fees": _f(leg.fees, 2),
            "price_age_seconds": round(age_seconds(book, now), 1),
        })
    contracts_cost = sum((leg.cost for leg in trade.legs), Decimal("0"))
    total_cost = contracts_cost + trade.fees
    profit = trade.estimated_profit
    ret = profit / total_cost if total_cost else Decimal("0")
    return {
        "quantity": trade.quantity,
        "legs": legs,
        "contracts_cost": _f(contracts_cost, 2),
        "fees": _f(trade.fees, 2),
        "total_cost": _f(total_cost, 2),
        "payout": trade.quantity,
        "profit": _f(profit, 2),
        "return_pct": _f(ret * 100, 2),
        "annualized_pct": _f(ret * 100 * 365 / days, 2) if days else None,
        "days_to_resolution": days,
    }


def pair_detail(con: sqlite3.Connection, pair_id: str, pairs=None, now: datetime | None = None) -> dict | None:
    now = now or utcnow()
    pairs = load_pairs(active_only=False) if pairs is None else pairs
    pair = next((p for p in pairs if p["id"] == pair_id), None)
    if pair is None:
        return None
    books = load_books(con)
    best = best_direction(pair, books, now)
    days = days_until(pair.get("resolution_date"), now)
    detail = {
        **pair_meta(pair),
        "kalshi_ticker": pair.get("kalshi_ticker"),
        "poly_slug": pair.get("poly_slug"),
        "kalshi_rules_url": pair.get("kalshi_rules_url"),
        "poly_rules_url": pair.get("poly_rules_url"),
        "rule_notes": pair.get("rule_notes", ""),
        "days_to_resolution": days,
        "directions": [evaluate_direction(f, s, now, pair.get("rules_verified") is True, fee_model=pair_fee_model(pair))
                       for f, s in directions_for(pair_id, books)],
        "best": best[0] if best else None,
        "status": best[0]["status"] if best else "no_data",
        "books": [],
        "trade_plan": None,
    }
    if best:
        result, first, second = best
        detail["books"] = [book_json(first, now), book_json(second, now)]
        if result["status"] == "trade_plan_ready":
            detail["trade_plan"] = trade_plan(first, second, days, now, fee_model=pair_fee_model(pair))
    return detail


def effective_entry(entry: dict, now: datetime) -> dict:
    """An 'open' gap the poller hasn't touched lately means the poller stopped. Show it as stale."""
    if entry["end_reason"] == "open" and entry.get("last_seen_at"):
        quiet = (now - opp.parse_time(entry["last_seen_at"])).total_seconds()
        if quiet > opp.MAX_SILENCE_SECONDS:
            return {**entry, "end_reason": "stale"}
    return entry


def list_opportunities(con: sqlite3.Connection, limit: int = 100, pairs=None,
                       now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    pairs = load_pairs(active_only=False) if pairs is None else pairs
    names = {p["id"]: p.get("name", p["id"]) for p in pairs}
    out = []
    rows = con.execute(
        "SELECT data FROM opportunities ORDER BY opened_at DESC LIMIT ?", (max(1, min(limit, 1000)),))
    for row in rows:
        entry = effective_entry(json.loads(row["data"]), now)
        entry["name"] = names.get(entry["pair_id"], entry["pair_id"])
        out.append(entry)
    return out


def results(con: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or utcnow()
    entries = [effective_entry(json.loads(r["data"]), now) for r in con.execute("SELECT data FROM opportunities")]
    survivors = [e for e in entries if e["result"] in ("tradable", "below_minimum")]
    tradable = [e for e in entries if e["result"] == "tradable"]

    def median(values):
        return round(statistics.median(values), 6) if values else None

    profits = [float(e["best_trade_profit"]) for e in survivors]
    return {
        "gaps_before_fees": len(entries),
        "survived_fees": len(survivors),
        "tradable": len(tradable),
        "open_now": sum(1 for e in entries if e["end_reason"] == "open"),
        "median_net_edge": median([float(e["best_net_edge"]) for e in survivors]),
        "median_size": median([e["max_size"] for e in survivors]),
        "median_profit": median(profits),
        "largest_profit": max(profits) if profits else None,
        "first_seen": min((e["opened_at"] for e in entries), default=None),
        "fee_model": "poller.fees.reviewed_pair_fees",
    }


def health(con: sqlite3.Connection, pairs=None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    pairs = load_pairs() if pairs is None else pairs
    last = con.execute("SELECT MAX(fetched_at) FROM latest_books").fetchone()[0]
    open_now = con.execute("SELECT COUNT(*) FROM opportunities WHERE end_reason = 'open'").fetchone()[0]
    return {
        "ok": True,
        "now": now.isoformat(),
        "last_book_at": last,
        "seconds_since_last_book": round((now - opp.parse_time(last)).total_seconds(), 1) if last else None,
        "active_pairs": len(pairs),
        "open_opportunities": open_now,
        "max_price_age_seconds": opp.MAX_AGE_SECONDS,
    }
