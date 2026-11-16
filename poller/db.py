"""SQLite storage shared by the poller (writes) and the website API (reads).

One file, data/arbcheck.db, holds what the website needs:
  latest_books   the newest order book for every pair, exchange, and side
  opportunities  every gap the logger has seen, open or finished

Raw API responses stay as files in data/raw. They don't go in here.

Load gaps from data you saved before the database existed:

    python -m poller.db --backfill
"""

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("ARBCHECK_DB", "data/arbcheck.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS latest_books (
    pair_id    TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    market     TEXT,
    asks       TEXT NOT NULL,   -- JSON list of [price, size], best price first
    fetched_at TEXT NOT NULL,   -- UTC, ISO format
    PRIMARY KEY (pair_id, exchange, outcome)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id           TEXT PRIMARY KEY,  -- pair_id|direction|opened_at
    pair_id      TEXT NOT NULL,
    direction    TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    last_seen_at TEXT,
    closed_at    TEXT,
    end_reason   TEXT NOT NULL,     -- open, gap_closed, stale, still_open
    result       TEXT NOT NULL,     -- tradable, below_minimum, fees_wiped_it_out
    data         TEXT NOT NULL      -- the full log entry as JSON
);

CREATE INDEX IF NOT EXISTS opportunities_by_time ON opportunities (opened_at);
"""


def connect(path=None) -> sqlite3.Connection:
    """Open the database, creating it and its tables if needed.

    WAL mode lets the API read while the poller writes.
    """
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    return con


def opportunity_id(entry: dict) -> str:
    return f'{entry["pair_id"]}|{entry["direction"]}|{entry["opened_at"]}'


def save_books(con: sqlite3.Connection, books) -> None:
    """Keep the newest book for each pair, exchange, and side."""
    rows = [
        (b.pair_id, b.exchange, b.outcome, b.market,
         json.dumps([[str(level.price), str(level.size)] for level in b.asks]),
         b.fetched_at)
        for b in books
    ]
    con.executemany(
        """
        INSERT INTO latest_books (pair_id, exchange, outcome, market, asks, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (pair_id, exchange, outcome) DO UPDATE SET
            market = excluded.market,
            asks = excluded.asks,
            fetched_at = excluded.fetched_at
        WHERE excluded.fetched_at >= latest_books.fetched_at
        """,
        rows,
    )


def save_opportunities(con: sqlite3.Connection, entries, overwrite: bool = True) -> None:
    """Insert or update opportunity entries. With overwrite=False, existing rows are kept."""
    rows = [
        (opportunity_id(e), e["pair_id"], e["direction"], e["opened_at"], e.get("last_seen_at"),
         e.get("closed_at"), e["end_reason"], e["result"], json.dumps(e, sort_keys=True))
        for e in entries
    ]
    if overwrite:
        sql = """
            INSERT INTO opportunities
                (id, pair_id, direction, opened_at, last_seen_at, closed_at, end_reason, result, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                closed_at = excluded.closed_at,
                end_reason = excluded.end_reason,
                result = excluded.result,
                data = excluded.data
        """
    else:
        sql = """
            INSERT OR IGNORE INTO opportunities
                (id, pair_id, direction, opened_at, last_seen_at, closed_at, end_reason, result, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
    con.executemany(sql, rows)


def mark_open_as_stale(con: sqlite3.Connection) -> int:
    """Gaps left 'open' by a poller that stopped without saying so can't be trusted. Call at startup."""
    rows = con.execute("SELECT id, data FROM opportunities WHERE end_reason = 'open'").fetchall()
    for row in rows:
        data = json.loads(row["data"])
        data["end_reason"] = "stale"
        con.execute("UPDATE opportunities SET end_reason = 'stale', data = ? WHERE id = ?",
                    (json.dumps(data, sort_keys=True), row["id"]))
    con.commit()
    return len(rows)


def save_cycle(con: sqlite3.Connection, books, closed, open_entries) -> None:
    """Everything from one poll cycle. Errors are logged so the poller keeps running."""
    try:
        save_books(con, books)
        save_opportunities(con, list(closed) + list(open_entries))
        con.commit()
    except Exception:
        logger.exception("Could not save this cycle to the database; the poller keeps running")
        try:
            con.rollback()
        except Exception:
            pass


def backfill(con: sqlite3.Connection, raw_dir: Path = Path("data/raw"), date: str | None = None) -> int:
    """Replay saved raw files and add any gaps the database doesn't have yet."""
    from poller.opportunities import index_raw_files, load_records, replay

    entries = replay(load_records(index_raw_files(raw_dir, date)))
    save_opportunities(con, entries, overwrite=False)
    con.commit()
    return len(entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ArbCheck database tools")
    parser.add_argument("--backfill", action="store_true",
                        help="load gaps from saved raw data that the database doesn't have yet")
    parser.add_argument("--raw", default="data/raw", help="folder of saved raw files")
    parser.add_argument("--date", help="only backfill this UTC date, YYYY-MM-DD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    con = connect()
    if args.backfill:
        found = backfill(con, Path(args.raw), args.date)
        print(f"Replayed saved data and found {found} gaps. New ones were added to {DB_PATH}")
    books = con.execute("SELECT COUNT(*) FROM latest_books").fetchone()[0]
    opps = con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    print(f"{DB_PATH}: {books} latest books, {opps} opportunities")
    con.close()


if __name__ == "__main__":
    main()
