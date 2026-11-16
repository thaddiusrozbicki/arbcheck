# ArbCheck website

The pages in this folder are the live site. They're plain HTML and CSS plus one script, `app.js`, which asks the API for fresh data every few seconds. Every address in it is relative (`/api/...`), so the same files work on localhost and on the VPS.

## How it fits together

```
poller (python -m poller.run)  writes  data/arbcheck.db
API    (uvicorn api.main:app)  reads   data/arbcheck.db, serves /api/... and these pages
browser                        calls   /api/... every 5 to 15 seconds
```

## Running it

From the repo root, in two terminals:

```
python -m poller.run
uvicorn api.main:app --reload --port 8000
```

Then open port 8000. To load gaps from data saved before the database existed, run `python -m poller.db --backfill` once.

## Pages

| Page | File | Data from | Refreshes |
|---|---|---|---|
| Pairs | `index.html` | `/api/pairs` | every 5 seconds |
| Pair detail | `pair.html?id=PAIR_ID` | `/api/pairs/PAIR_ID` | every 5 seconds |
| Opportunities | `opportunities.html` | `/api/opportunities` | every 10 seconds |
| Results | `results.html` | `/api/results` | every 15 seconds |
| Admin | `admin.html` | not connected yet | |
| Admin sign in | `login.html` | not connected yet | |

The header on every page reads `/api/health` and says whether the poller is feeding data.

## Rules the site follows

These match the opportunity logger, because the API uses the same code.

- A price older than 5 seconds never produces a trade plan. Ages count up in the browser between refreshes.
- A trade plan needs at least 1 cent of edge per pair after fees.
- A pair with `"verified": false` in `pairs.json` never gets a trade plan.
- Fees come from `poller/fees.py` and sizes come from `poller/sizing.py`, so the numbers change when those do.
- Scanner only. No buy buttons, no accounts, no money.
