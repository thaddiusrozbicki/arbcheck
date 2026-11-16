"""In-memory order books kept current by the WebSocket feeds.

A feed gets one full snapshot of each book, then a stream of changes. LiveBook
applies both and can turn itself into the same records the REST poller produces,
so the existing normalizers and engine work on WebSocket books unchanged.

Every book has a state the website can show:
    live          snapshot received and every change since applied
    resyncing     we know the book may be wrong and are waiting for a new snapshot
    disconnected  the connection is down

Only live books are ever handed to the engine.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

LIVE, RESYNCING, DISCONNECTED = "live", "resyncing", "disconnected"
ZERO = Decimal("0")


def _decimal(value, what):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} must be a number, got {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{what} must be finite, got {value!r}")
    return result


class LiveBook:
    """One market's price levels. sides maps a side name to {price: size}."""

    def __init__(self, sides):
        self.sides = {side: {} for side in sides}
        self.state = DISCONNECTED
        self.updated_at = None

    def _check(self, side, price, size):
        if side not in self.sides:
            raise ValueError(f"Unknown side {side!r}")
        price, size = _decimal(price, "price"), _decimal(size, "size")
        if not ZERO <= price <= 1:
            raise ValueError(f"Price must be in [0, 1], got {price}")
        return price, size

    def _set(self, side, price, size):
        price, size = self._check(side, price, size)
        if size < 0:
            raise ValueError(f"Size must be nonnegative, got {size}")
        if size == 0:
            self.sides[side].pop(price, None)
        else:
            self.sides[side][price] = size

    def apply_snapshot(self, levels, at: datetime):
        """Replace the whole book. levels maps side to [(price, size), ...]."""
        fresh = LiveBook(self.sides)
        for side, rows in levels.items():
            for price, size in rows or []:
                fresh._set(side, price, size)
        self.sides = fresh.sides
        self.state = LIVE
        self.updated_at = at

    def set_level(self, side, price, size, at: datetime):
        """Set the size at one price. Size 0 removes the level."""
        self._set(side, price, size)
        self.updated_at = at

    def add_to_level(self, side, price, delta, at: datetime):
        """Add delta contracts at one price. Going below zero means we missed an update."""
        price, delta = self._check(side, price, delta)
        size = self.sides[side].get(price, ZERO) + delta
        if size < 0:
            raise ValueError(f"Level {side} {price} went negative ({size})")
        self._set(side, price, size)
        self.updated_at = at

    def best(self, side, *, highest):
        prices = self.sides[side]
        if not prices:
            return None
        return max(prices) if highest else min(prices)

    def levels(self, side):
        """[[price, size], ...] as strings, cheapest first, like the exchanges' REST books."""
        return [[str(price), str(size)] for price, size in sorted(self.sides[side].items())]

    def mark(self, state):
        self.state = state


class RawLog:
    """Appends every WebSocket message to data/raw/<name>/YYYY-MM-DD/HH.jsonl.

    One line per message keeps the file count low (24 a day per feed) and the
    nightly compression job packs each finished day like the REST files.
    """

    def __init__(self, root, name):
        self.folder = Path(root) / name

    def write(self, text, at: datetime):
        path = self.folder / at.strftime("%Y-%m-%d") / (at.strftime("%H") + ".jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"received_at": at.isoformat(), "raw": text}, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("Could not save raw message to %s", path)
