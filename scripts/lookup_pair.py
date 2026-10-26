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