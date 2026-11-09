"""Opportunity log: every price gap the scanner sees, from when it opens to when it closes.

An opportunity is one pair in one direction (for example Kalshi YES + Polymarket NO)
where the best YES ask plus the best NO ask is under $1.00 before fees. It gets
logged even if fees wipe it out.

Live: the poller runs this after every cycle and appends each opportunity to
data/derived/opportunities.jsonl the moment it closes.

    python -m poller.run

Replay: rebuild a log from saved raw data. The same input always gives the same log,
which is how we check the math.

    python -m poller.opportunities
    python -m poller.opportunities --date 2026-11-09

Each line of a log is one opportunity. See OpportunityTracker._entry for the fields.
"""

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from poller import fees
from poller.orderbook import OrderBook, normalize_kalshi, normalize_polymarket
from poller.sizing import size_direction

logger = logging.getLogger(__name__)

ONE = Decimal("1")
ZERO = Decimal("0")
MIN_EDGE = Decimal("0.01")   # 1 cent per pair after fees, from the project brief
MAX_AGE_SECONDS = 5          # older prices never count
CYCLE_GAP_SECONDS = 3        # files further apart than this belong to different poll cycles
MAX_CONTRACTS = 10_000       # passed to size_direction so sizing stays fast on deep books
LIVE_LOG = Path("data/derived/opportunities.jsonl")
REPLAY_LOG = Path("data/derived/opportunities_replay.jsonl")

DIRECTIONS = (
    (("kalshi", "YES"), ("polymarket", "NO")),
    (("kalshi", "NO"), ("polymarket", "YES")),
)


# ---------- fees ----------
# The fee model belongs to poller/fees.py. When the real one lands, point this at it.
FEE_MODEL = fees.sample_fee


# ---------- reading saved raw files ----------

def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def books_from_raw(record: dict) -> list[OrderBook]:
    """Turn one saved raw file into shared-format order books. Returns [] if unusable."""
    if record.get("http_status") != 200 or not record.get("raw_body"):
        return []
    try:
        body = json.loads(record["raw_body"])
        if record.get("exchange") == "kalshi":
            book = body.get("orderbook_fp")
            if book is None:
                return []
            return normalize_kalshi({**record, "book": book})
        if record.get("exchange") == "polymarket" and record.get("outcome") in ("YES", "NO"):
            if "asks" not in body or "asset_id" not in body:
                return []
            return normalize_polymarket({**record, "book": body})
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Skipping unreadable file for %s: %s", record.get("pair_id"), exc)
    return []


DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def index_raw_files(raw_dir: Path, date: str | None = None) -> list[tuple[str, Path]]:
    """(fetched_at, path) for every order book file under raw_dir, oldest first.

    Only the timestamps are kept in memory, so a week of data doesn't fill up RAM.
    Gamma metadata files are skipped.
    """
    index = []
    for path in raw_dir.rglob("*.json"):
        if "gamma" in path.parts:
            continue
        if date and any(DATE_FOLDER.match(part) and part != date for part in path.parts):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("Skipping unreadable file %s", path)
            continue
        fetched = record.get("fetched_at")
        if not fetched or not record.get("pair_id"):
            continue
        if date and not fetched.startswith(date):
            continue
        index.append((fetched, path))
    index.sort()
    return index


def load_records(index: list[tuple[str, Path]]):
    """Read the indexed files one at a time, oldest first."""
    for _, path in index:
        yield json.loads(path.read_text())


def read_raw_files(raw_dir: Path, date: str | None = None) -> list[dict]:
    """Every order book file under raw_dir as a list, oldest first. Handy for small folders."""
    return list(load_records(index_raw_files(raw_dir, date)))


def split_into_cycles(records):
    """Group time-ordered files into poll cycles. A new cycle starts after a quiet gap."""
    current, last = [], None
    for record in records:
        at = parse_time(record["fetched_at"])
        if last is not None and (at - last).total_seconds() > CYCLE_GAP_SECONDS:
            yield current
            current = []
        current.append(record)
        last = at
    if current:
        yield current


# ---------- checking one direction ----------

@dataclass(frozen=True)
class Observation:
    """What one direction of one pair looked like at one moment."""
    pair_id: str
    direction: str
    observed_at: datetime
    first_price: Decimal
    second_price: Decimal
    first_age: float
    second_age: float
    gross_edge: Decimal        # per pair, before fees, at the best prices
    fee: Decimal               # per pair, at the best prices
    net_edge: Decimal          # per pair, after fees, at the best prices
    size: int                  # pairs available before fees, from the sizing engine
    gross_profit: Decimal      # best total profit before fees, from the sizing engine
    best_size: int             # sizing engine's best size after fees (0 if fees wipe it out)
    best_profit: Decimal       # sizing engine's profit after fees at best_size
    size_capped: bool


def observe(first: OrderBook, second: OrderBook, at: datetime, fee_model=None) -> Observation | None:
    """Return an Observation if this direction has a gap right now, otherwise None.

    Size and profit come from the sizing engine. Fees come from the fee model.
    """
    fee_model = fee_model or FEE_MODEL
    if not first.asks or not second.asks:
        return None
    p1, p2 = first.asks[0].price, second.asks[0].price
    gross_edge = ONE - p1 - p2
    if gross_edge <= 0:
        return None
    sizing = size_direction(first, second, fee_model, max_contracts=MAX_CONTRACTS)
    size = sizing.gross_opportunity.quantity
    if size < 1:
        return None
    fee = fee_model(first.exchange, p1, ONE) + fee_model(second.exchange, p2, ONE)
    return Observation(
        pair_id=first.pair_id,
        direction=f"{first.exchange} {first.outcome} + {second.exchange} {second.outcome}",
        observed_at=at,
        first_price=p1,
        second_price=p2,
        first_age=(at - parse_time(first.fetched_at)).total_seconds(),
        second_age=(at - parse_time(second.fetched_at)).total_seconds(),
        gross_edge=gross_edge,
        fee=fee,
        net_edge=gross_edge - fee,
        size=size,
        gross_profit=sizing.gross_opportunity.gross_profit,
        best_size=sizing.best_trade.quantity,
        best_profit=sizing.best_trade.estimated_profit,
        size_capped=size >= MAX_CONTRACTS,
    )


def is_fresh(book: OrderBook, at: datetime, max_age: float = MAX_AGE_SECONDS) -> bool:
    return (at - parse_time(book.fetched_at)).total_seconds() <= max_age


# ---------- the tracker ----------

def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


class OpportunityTracker:
    """Keeps one open entry per pair and direction, and closes it when the gap goes away."""

    def __init__(self, min_edge: Decimal = MIN_EDGE):
        self.min_edge = min_edge
        self.open: dict[tuple[str, str], dict] = {}

    def update(self, pair_id: str, direction: str, at: datetime,
               obs: Observation | None, stale: bool = False) -> list[dict]:
        """Feed in one check. Returns any entries that just closed."""
        key = (pair_id, direction)
        entry = self.open.get(key)
        if stale:
            if entry:
                del self.open[key]
                return [self._entry(entry, closed_at=None, end_reason="stale")]
            return []
        if obs is None:
            if entry:
                del self.open[key]
                return [self._entry(entry, closed_at=at, end_reason="gap_closed")]
            return []
        if entry is None:
            self.open[key] = {"first": obs, "last": obs, "best_edge": obs,
                              "biggest": obs, "cycles": 1}
        else:
            entry["last"] = obs
            entry["cycles"] += 1
            if obs.net_edge > entry["best_edge"].net_edge:
                entry["best_edge"] = obs
            if obs.size > entry["biggest"].size:
                entry["biggest"] = obs
        return []

    def close_missing(self, seen: set[tuple[str, str]]) -> list[dict]:
        """Close open entries whose pair got no fresh check this cycle."""
        closed = []
        for key in [k for k in self.open if k not in seen]:
            closed.append(self._entry(self.open.pop(key), closed_at=None, end_reason="stale"))
        return closed

    def finish(self) -> list[dict]:
        """Entries still open when the data runs out."""
        entries = [self._entry(e, closed_at=None, end_reason="still_open")
                   for e in self.open.values()]
        self.open.clear()
        return entries

    def result(self, net_edge: Decimal) -> str:
        if net_edge >= self.min_edge:
            return "tradable"
        if net_edge > 0:
            return "below_minimum"
        return "fees_wiped_it_out"

    def _entry(self, entry: dict, closed_at: datetime | None, end_reason: str) -> dict:
        first, last, best, big = entry["first"], entry["last"], entry["best_edge"], entry["biggest"]
        return {
            "pair_id": first.pair_id,
            "direction": first.direction,
            "opened_at": first.observed_at.isoformat(),
            "last_seen_at": last.observed_at.isoformat(),
            "closed_at": closed_at.isoformat() if closed_at else None,
            "end_reason": end_reason,
            "checks_seen": entry["cycles"],
            "result": self.result(best.net_edge),
            "opening_prices": [_money(first.first_price), _money(first.second_price)],
            "best_prices": [_money(best.first_price), _money(best.second_price)],
            "best_gross_edge": _money(best.gross_edge),
            "best_fee": _money(best.fee),
            "best_net_edge": _money(best.net_edge),
            "price_ages_at_best": [round(best.first_age, 3), round(best.second_age, 3)],
            "max_size": big.size,
            "max_gross_profit": _money(big.gross_profit),
            "best_trade_size": best.best_size,
            "best_trade_profit": _money(best.best_profit),
            "size_capped": big.size_capped,
        }


# ---------- checking a whole poll cycle (shared by live and replay) ----------

class CycleChecker:
    """Feed it the books from each poll cycle. It checks every pair in both directions."""

    def __init__(self, tracker: OpportunityTracker | None = None, fee_model=None):
        self.tracker = tracker or OpportunityTracker()
        self.fee_model = fee_model or FEE_MODEL
        self.latest: dict[tuple[str, str, str], OrderBook] = {}
        self.just_opened: list[Observation] = []

    def check_cycle(self, books: list[OrderBook]) -> list[dict]:
        """Check one cycle's books. Returns entries that closed during this cycle."""
        self.just_opened = []
        touched: dict[str, datetime] = {}
        for book in books:
            self.latest[(book.pair_id, book.exchange, book.outcome)] = book
            at = parse_time(book.fetched_at)
            if book.pair_id not in touched or at > touched[book.pair_id]:
                touched[book.pair_id] = at

        closed, seen = [], set()
        for pair_id in sorted(touched):
            at = touched[pair_id]
            for first_key, second_key in DIRECTIONS:
                first = self.latest.get((pair_id, *first_key))
                second = self.latest.get((pair_id, *second_key))
                if first is None or second is None:
                    continue
                direction = f"{first_key[0]} {first_key[1]} + {second_key[0]} {second_key[1]}"
                key = (pair_id, direction)
                seen.add(key)
                if not (is_fresh(first, at) and is_fresh(second, at)):
                    closed += self.tracker.update(pair_id, direction, at, None, stale=True)
                    continue
                obs = observe(first, second, at, self.fee_model)
                was_open = key in self.tracker.open
                closed += self.tracker.update(pair_id, direction, at, obs)
                if obs is not None and not was_open:
                    self.just_opened.append(obs)
        closed += self.tracker.close_missing(seen)
        return closed

    def finish(self) -> list[dict]:
        """Entries still open when we stop watching."""
        return self.tracker.finish()


def replay(records, tracker: OpportunityTracker | None = None,
           fee_model=None) -> list[dict]:
    """Run time-ordered raw records through the checker and return every opportunity entry."""
    checker = CycleChecker(tracker, fee_model)
    entries: list[dict] = []
    for cycle in split_into_cycles(records):
        books = [book for record in cycle for book in books_from_raw(record)]
        entries += checker.check_cycle(books)
    entries += checker.finish()
    entries.sort(key=lambda e: (e["opened_at"], e["pair_id"], e["direction"]))
    return entries


# ---------- live logging (called from poller/run.py) ----------

def append_log(entries: list[dict], out: Path = LIVE_LOG) -> None:
    if not entries:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")


def log_cycle(checker: CycleChecker, books: list[OrderBook], out: Path = LIVE_LOG) -> None:
    """Check one live cycle and save anything that closed. Never stops the poller."""
    try:
        closed = checker.check_cycle(books)
        append_log(closed, out)
    except Exception:
        logger.exception("Opportunity check failed this cycle; the poller keeps running")
        return
    for obs in checker.just_opened:
        logger.info("Opportunity opened: %s %s, %.1f + %.1f cents, %.2f cents after fees",
                    obs.pair_id, obs.direction, float(obs.first_price * 100),
                    float(obs.second_price * 100), float(obs.net_edge * 100))
    for entry in closed:
        logger.info("Opportunity closed: %s %s, %s, %s",
                    entry["pair_id"], entry["direction"], entry["end_reason"], entry["result"])


def finish_log(checker: CycleChecker, out: Path = LIVE_LOG) -> None:
    """Save gaps that were still open when the poller stopped."""
    try:
        append_log(checker.finish(), out)
    except Exception:
        logger.exception("Could not save the still-open opportunities")


def write_log(entries: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the opportunity log from saved raw data")
    parser.add_argument("--raw", default="data/raw", help="folder of saved raw files")
    parser.add_argument("--out", default=str(REPLAY_LOG), help="where to write the replayed log")
    parser.add_argument("--date", help="only use files fetched on this UTC date, YYYY-MM-DD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    index = index_raw_files(Path(args.raw), args.date)
    entries = replay(load_records(index))
    write_log(entries, Path(args.out))

    counts = {}
    for e in entries:
        counts[e["result"]] = counts.get(e["result"], 0) + 1
    still_open = sum(1 for e in entries if e["end_reason"] == "still_open")
    print(f"Read {len(index)} order book files")
    print(f"Fee model: poller.fees.{FEE_MODEL.__name__}")
    print(f"Found {len(entries)} opportunities: "
          f"{counts.get('tradable', 0)} tradable, "
          f"{counts.get('below_minimum', 0)} below the 1 cent minimum, "
          f"{counts.get('fees_wiped_it_out', 0)} wiped out by fees")
    print(f"Still open at the end of the data: {still_open}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
