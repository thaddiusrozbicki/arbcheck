"""Find the IDs you need for pairs.json without clicking through raw JSON.

Run from the repo root. Every command uses public endpoints, no keys.

  1. See every market inside a Kalshi event (event ticker or a market ticker):
       python scripts/lookup_pair.py kalshi KXNFLGAME-26NOV01KCBUF

  2. See every market inside a Polymarket event (slug or full URL):
       python scripts/lookup_pair.py poly https://polymarket.com/event/some-event-slug

     Can't find it on the site? Search Polymarket by keywords instead:
       python scripts/lookup_pair.py search "chiefs bills"

  3. Once you know the exact market on each side, print a pairs.json entry:
       python scripts/lookup_pair.py stub --kalshi KXNFLGAME-26NOV01KCBUF-KC \\
           --poly some-market-slug --poly-yes "Chiefs"

     --poly-yes is only needed when the Polymarket outcomes aren't Yes/No
     (sports markets often use team names). Pick the outcome that means the
     same thing as YES on the Kalshi market.

The stub leaves rule_notes empty on purpose. Read both rule pages and write it
yourself. Double check resolution_date too, since it comes from Kalshi's close
time in UTC.
"""

import argparse
import json
import re
import sys
from urllib.parse import urlparse

import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
TIMEOUT = 15
HEADERS = {"User-Agent": "ArbCheck/0.1 (TAMID SWEF student project)"}


class LookupFailed(Exception):
    """Something we can explain to the user in one line."""


# ---------- helpers that don't touch the network (these are unit tested) ----------

def extract_poly_slug(text):
    """Accept a bare slug or any Polymarket URL and return the slug.

    The slug is always the last part of the path, which is the most specific:
    polymarket.com/event/<event-slug>             -> event-slug
    polymarket.com/event/<event-slug>/<market>    -> market
    polymarket.us/sports/wta/<market-slug>        -> market-slug
    """
    text = text.strip()
    if "polymarket." not in text and "://" not in text:
        return text.strip("/")
    url = text if "://" in text else "https://" + text
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        raise LookupFailed(f"Couldn't find a slug in {text}")
    return parts[-1]


def is_polymarket_us(text):
    return "polymarket.us" in text.lower()


US_NOTE = (
    "Note: that's a Polymarket US link. ArbCheck reads international Polymarket data "
    "(the Gamma and CLOB APIs), so this looks up the same slug there."
)


def parse_json_list(value):
    """Gamma returns outcomes and clobTokenIds as JSON strings. Handle both."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return []


def map_poly_tokens(market, yes_outcome=None):
    """Return (yes_token, no_token) for a binary Polymarket market."""
    outcomes = parse_json_list(market.get("outcomes"))
    tokens = parse_json_list(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(tokens) != 2:
        raise LookupFailed(
            f"Expected 2 outcomes and 2 tokens, got {outcomes} / {len(tokens)} tokens"
        )

    lowered = [o.lower() for o in outcomes]
    if yes_outcome is None:
        if "yes" in lowered and "no" in lowered:
            yes_outcome = "yes"
        else:
            raise LookupFailed(
                f"Outcomes are {outcomes}, not Yes/No. Rerun with "
                f'--poly-yes "<the outcome that matches Kalshi YES>"'
            )
    if yes_outcome.lower() not in lowered:
        raise LookupFailed(f'"{yes_outcome}" is not one of {outcomes}')

    yes_i = lowered.index(yes_outcome.lower())
    return tokens[yes_i], tokens[1 - yes_i]


def slugify(text, max_len=60):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def kalshi_series_from_ticker(ticker):
    """KXNFLGAME-26NOV01KCBUF-KC -> KXNFLGAME (the series is the first chunk)."""
    return ticker.split("-")[0]


def kalshi_name(market):
    title = (market.get("title") or "").strip()
    side = (market.get("yes_sub_title") or "").strip()
    if side and side.lower() != title.lower():
        return f"{title} ({side})"
    return title


def build_stub(kalshi_market, poly_market, yes_token, no_token, pair_id=None, category="other"):
    """Turn the two API responses into a pairs.json entry."""
    ticker = kalshi_market["ticker"]
    name = kalshi_name(kalshi_market) or poly_market.get("question", ticker)
    close = kalshi_market.get("close_time") or ""

    events = poly_market.get("events") or []
    event_slug = events[0].get("slug") if events and isinstance(events[0], dict) else None
    poly_url_slug = event_slug or poly_market.get("slug", "")

    return {
        "id": pair_id or slugify(name),
        "name": name,
        "category": category,
        "kalshi_ticker": ticker,
        "poly_slug": poly_market.get("slug", ""),
        "poly_yes_token": str(yes_token),
        "poly_no_token": str(no_token),
        "resolution_date": close[:10],
        "kalshi_rules_url": f"https://kalshi.com/markets/{kalshi_series_from_ticker(ticker).lower()}",
        "poly_rules_url": f"https://polymarket.com/event/{poly_url_slug}",
        "rule_notes": "",
        "active": True,
    }


# ---------- network calls ----------

def get_json(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise LookupFailed(f"Couldn't reach {url} ({type(e).__name__}). Check your connection.")
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise LookupFailed("Rate limited. Wait a minute and try again.")
    if not r.ok:
        raise LookupFailed(f"{url} returned HTTP {r.status_code}")
    return r.json()


def kalshi_event_markets(event_ticker):
    data = get_json(f"{KALSHI_API}/events/{event_ticker}", {"with_nested_markets": "true"})
    if not data:
        return None, []
    event = data.get("event") or {}
    markets = event.get("markets") or data.get("markets") or []
    return event, markets


def kalshi_market(ticker):
    """Fetch one market. Falls back to the parent event if the direct call 404s."""
    data = get_json(f"{KALSHI_API}/markets/{ticker}")
    if data and data.get("market"):
        return data["market"]
    if "-" in ticker:
        _, markets = kalshi_event_markets(ticker.rsplit("-", 1)[0])
        for m in markets:
            if m.get("ticker") == ticker:
                return m
    raise LookupFailed(f"Kalshi market {ticker} not found. Check the ticker.")


def poly_markets(slug):
    """Return the market(s) for a slug. Tries it as a market, then as an event."""
    data = get_json(f"{GAMMA_API}/markets", {"slug": slug})
    if data:
        return data
    data = get_json(f"{GAMMA_API}/events", {"slug": slug})
    if data:
        return data[0].get("markets") or []
    raise LookupFailed(
        f"Nothing on Polymarket for slug '{slug}'. Try searching instead:\n"
        f'  python scripts/lookup_pair.py search "a few words from the market"'
    )


# ---------- commands ----------

def cmd_kalshi(args):
    ticker = args.ticker.strip().upper()
    event, markets = kalshi_event_markets(ticker)
    if not markets:
        # maybe they passed a market ticker, so try its parent event
        m = kalshi_market(ticker)
        event, markets = kalshi_event_markets(m.get("event_ticker") or ticker.rsplit("-", 1)[0])
        markets = markets or [m]

    event = event or {}
    print(f"\nKalshi event: {event.get('title', '?')}  [{event.get('event_ticker', ticker)}]")
    print(f"Rules page:   https://kalshi.com/markets/{kalshi_series_from_ticker(ticker).lower()}\n")
    for m in markets:
        print(f"  {m.get('ticker')}")
        print(f"      {kalshi_name(m)}")
        print(f"      status={m.get('status')}  closes={m.get('close_time')}")
    print("\nCopy the market ticker you want into the stub command.")


def cmd_poly(args):
    if is_polymarket_us(args.slug):
        print(US_NOTE, file=sys.stderr)
    slug = extract_poly_slug(args.slug)
    markets = poly_markets(slug)
    print(f"\nPolymarket results for '{slug}':\n")
    for m in markets:
        outcomes = parse_json_list(m.get("outcomes"))
        tokens = parse_json_list(m.get("clobTokenIds"))
        print(f"  slug: {m.get('slug')}")
        print(f"      {m.get('question')}")
        print(f"      ends={m.get('endDate')}  closed={m.get('closed')}")
        for o, t in zip(outcomes, tokens):
            print(f"      {o:>12} -> {t}")
    print("\nUse the market slug (not the event slug) in the stub command.")


def cmd_search(args):
    data = get_json(f"{GAMMA_API}/public-search", {"q": args.query})
    events = (data or {}).get("events") or []
    if not events:
        raise LookupFailed(f'No Polymarket results for "{args.query}". Try fewer or different words.')

    print(f'\nPolymarket events matching "{args.query}":\n')
    for e in events[: args.limit]:
        print(f"  event slug: {e.get('slug')}")
        print(f"      {e.get('title')}")
        print(f"      ends={e.get('endDate')}  closed={e.get('closed')}")
        for m in e.get("markets") or []:
            if m.get("slug"):
                print(f"      market slug: {m.get('slug')}  ({m.get('question')})")
    print("\nPass an event or market slug to the poly command to see token IDs.")


def cmd_stub(args):
    k_market = kalshi_market(args.kalshi.strip().upper())

    if is_polymarket_us(args.poly):
        print(US_NOTE, file=sys.stderr)
    markets = poly_markets(extract_poly_slug(args.poly))
    if len(markets) != 1:
        options = "\n".join(f"  {m.get('slug')}: {m.get('question')}" for m in markets)
        raise LookupFailed(
            f"That slug has {len(markets)} markets. Pass one of these instead:\n{options}"
        )
    p_market = markets[0]

    yes_tok, no_tok = map_poly_tokens(p_market, args.poly_yes)
    stub = build_stub(k_market, p_market, yes_tok, no_tok, args.id, args.category)

    print("\nKalshi:     ", kalshi_name(k_market), f"(closes {k_market.get('close_time')})")
    print("Polymarket: ", p_market.get("question"), f"(ends {p_market.get('endDate')})")
    print("\nPaste this into pairs/pairs.json, then fill in rule_notes:\n")
    print(json.dumps(stub, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Look up IDs for pairs.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("kalshi", help="list markets in a Kalshi event")
    p.add_argument("ticker", help="event ticker or market ticker")
    p.set_defaults(func=cmd_kalshi)

    p = sub.add_parser("poly", help="list markets and token IDs for a Polymarket slug or URL")
    p.add_argument("slug", help="slug or polymarket.com URL")
    p.set_defaults(func=cmd_poly)

    p = sub.add_parser("search", help="search Polymarket events by keywords")
    p.add_argument("query", help='keywords in quotes, e.g. "chiefs bills"')
    p.add_argument("--limit", type=int, default=10, help="max events to show")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stub", help="print a ready-to-paste pairs.json entry")
    p.add_argument("--kalshi", required=True, help="Kalshi MARKET ticker")
    p.add_argument("--poly", required=True, help="Polymarket MARKET slug or URL")
    p.add_argument("--poly-yes", help="Polymarket outcome that matches Kalshi YES")
    p.add_argument("--id", help="custom id (default is built from the name)")
    p.add_argument("--category", default="other",
                   help="sports, economics, politics, crypto, weather, culture, other")
    p.set_defaults(func=cmd_stub)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except LookupFailed as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())