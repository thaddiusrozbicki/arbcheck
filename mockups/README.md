# ArbCheck mockups (week 2)

Static HTML and CSS mockups of every Iteration 1 page, filled with sample data. Nothing here talks to the poller yet. In week 4 these pages get wired to the API and show live prices.

## How to view them

Open `index.html` in any browser.

In a codespace, run this from the repo root, then open port 8000 from the **Ports** tab.

```
python -m http.server 8000 --directory mockups
```

## Pages

| Page | File | What it's for | Who uses it |
|---|---|---|---|
| Pairs | `index.html` | Every matched pair with Kalshi and Polymarket prices side by side, the age of each price, and the current edge | Anyone |
| Pair detail | `pair.html` | One pair's order books on both exchanges, its best trade plan, rule links, and rule notes | Anyone |
| Opportunities | `opportunities.html` | Every gap with its start and end time, including the ones fees wiped out | Anyone |
| Results | `results.html` | How many gaps showed up before fees, how many survived fees, and how big the survivors were | Anyone |
| Admin | `admin.html` | Add a pair or pause one | Admin only |
| Admin sign in | `login.html` | The only login in the app | Admin only |

## Rules the design follows

These come from the Iteration 1 section of the project brief.

- Every price shows how old it is. A price older than 5 seconds never produces a trade plan, so the Avalanche row shows as stale with no edge.
- Every trade plan shows the contracts and prices on both sides, total cost, fees, payout, profit, return, annualized return, and the age of every price used.
- The engine checks both directions for each pair. The Pairs page shows whichever direction is cheaper.
- A trade plan needs at least 1¢ of edge per pair after fees.
- Scanner only. No buy buttons, no user accounts, no money.

## About the sample data

- Pair names, tickers, slugs, resolution dates, and rule notes come from `pairs/pairs.json`.
- Prices, ages, and opportunity counts are made up for the mockup.
- `pair.html` uses the order books from the example trade plan in the project brief and reproduces its numbers to the cent: 120 contracts, $117.64 total cost, $2.36 profit. The brief assumes 23 days to resolution, which gives about 32% annualized. This pair resolves in 38 days, so the page shows 19.3%.
- Only the Fed pair has a detail page. In week 4 every pair name on the Pairs page links to its own detail page.
- The Polymarket rules link on `pair.html` is a placeholder until it's wired to `poly_rules_url` in `pairs.json`.

## How to read the bars

The bar on each row of the Pairs page covers 94¢ to $1.04, with a line at $1.00.

- Light grey is what one YES plus one NO costs.
- Dark grey is the Kalshi fee per pair.
- Gold is the edge left under $1.00.
- Red is how far the pair goes over $1.00.
- Hatching means a price is too old to use.
