# ArbCheck

Real-time scanner for price gaps between Kalshi and Polymarket on the same event, and whether each gap survives fees, depth, and resolution risk.

TAMID SWEF, Fall 2026. Team is Steffia, Prabu, and Thaddius. Liaison is Issa.

**Scanner only.** No orders, no accounts, no real money. Every Polymarket leg stays simulated.

## Setup

Python 3.12.

```bash
git clone <repo-url>
cd arbcheck
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest                           # should pass before you change anything
```

## Layout

```
pairs/pairs.json          matched pairs (the list everything reads)
pairs/validate.py         checks pairs.json, runs in CI
pairs/loader.py           load_pairs(), use this instead of opening the file yourself
poller/kalshi.py          Kalshi order book poller
poller/polymarket.py      Polymarket order book poller
poller/run.py             runs both pollers on a loop
poller/orderbook.py       shared order book format and exchange converters
scripts/lookup_pair.py    finds tickers and token IDs for new pairs
tests/                    pytest tests
data/raw/                 raw API responses (gitignored, never committed)
```

## Shared order books (Week 2)

`poller.orderbook.OrderBook` is the format the pricing engine can consume:
`exchange`, `pair_id`, `market` (Kalshi ticker or Polymarket token ID),
`outcome` (`YES`/`NO`), `asks`, and the original UTC `fetched_at` string.
Each ask is an `AskLevel(price, size)`, using `Decimal` dollars and contracts.
Asks are sorted cheapest first; zero sizes are omitted and an empty tuple means
there is no available ask depth. Invalid prices or sizes reject the book.

`normalize_kalshi(record)` returns YES and NO books by complementing opposite
bids: a NO bid at $0.59 becomes a YES ask at $0.41, with the same size.
`normalize_polymarket(record)` returns one book for the record's outcome token.
Both consume the records returned by the existing collectors without changing
raw response storage. `poller.run.poll_once()` returns the combined normalized
books; the continuous runner currently logs collection and does not persist
normalized books.

## How we work

- Nobody pushes to `main`. Everything goes through a pull request with at least 1 review.
- Branch names look like `feature/kalshi-poller`, `fix/token-order`, `pairs/add-cpi`.
- Keep PRs small. One feature or fix per PR.
- CI runs `python -m pairs.validate` and `pytest` on every PR. Fix it if it's red.
- Raw data stays in `data/raw/` on whatever machine runs the poller. Never commit it.
- Secrets go in `.env` (copy `.env.example`). Never commit `.env`.

## The pairs file

`pairs/pairs.json` is a list of objects, one per matched pair.

```json
{
  "id": "kc-buf-2026-11-01-kc",
  "name": "Kansas City at Buffalo Winner? (Kansas City)",
  "category": "sports",
  "kalshi_ticker": "KXNFLGAME-26NOV01KCBUF-KC",
  "poly_slug": "nfl-kc-buf-2026-11-01",
  "poly_yes_token": "7111...",
  "poly_no_token": "7222...",
  "resolution_date": "2026-11-02",
  "kalshi_rules_url": "https://kalshi.com/markets/kxnflgame",
  "poly_rules_url": "https://polymarket.com/event/nfl-kc-buf-2026-11-01",
  "rule_notes": "Both settle on the official NFL result incl. OT. Kalshi voids if postponed past X, Polymarket resolves 50/50.",
  "active": true
}
```

(Example values are made up. Look up real ones with the script below.)

| Field | Meaning |
|---|---|
| `id` | Unique, lowercase-with-dashes. Never reuse or change once the poller is running. |
| `kalshi_ticker` | The **market** ticker (one specific outcome), not the series or event ticker. |
| `poly_yes_token` | Polymarket CLOB token that means the same thing as YES on Kalshi. |
| `poly_no_token` | The other token. |
| `resolution_date` | YYYY-MM-DD. |
| `rule_notes` | Where the two markets' rules differ. See checklist below. |
| `active` | `false` pauses the pair without deleting it. |

Categories are sports, economics, politics, crypto, weather, culture, or other.

### Adding a pair

```bash
git checkout -b pairs/add-something
python scripts/lookup_pair.py kalshi <EVENT_TICKER>       # list Kalshi markets
python scripts/lookup_pair.py poly <polymarket URL>        # list Polymarket markets + tokens
python scripts/lookup_pair.py stub --kalshi <MARKET_TICKER> --poly <MARKET_SLUG> [--poly-yes "<outcome>"] --category sports
```

Paste the stub into `pairs.json`, write the `rule_notes`, run `python -m pairs.validate`, and open a PR.

### Rule notes checklist

Compare both rule pages and note anything that differs.

- Resolution source (who decides the answer, e.g. BLS release, official league stats)
- Deadline and time zone
- Postponed, canceled, or delayed event
- Overtime, ties, and pushes for sports
- Threshold wording ("above" vs "at or above", rounding)
- Exact bracket for multi-outcome events

If the rules really differ, it's two separate bets and not an arb. Leave it out or set `active` to false.
