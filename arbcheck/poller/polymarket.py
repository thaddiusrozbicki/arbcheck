"""Polymarket poller. Owner: ______

Week 1 goal: for every active pair, fetch the CLOB order book for BOTH tokens
and save each raw response with a timestamp to data/raw/ BEFORE parsing.

What you read from pairs.json:  pair["poly_yes_token"], pair["poly_no_token"]
Get the pairs with:             from pairs import load_pairs

Public base URLs (no key needed):
    Gamma (market info):  https://gamma-api.polymarket.com
    CLOB (order books):   https://clob.polymarket.com

Read only. Per the project rules, every Polymarket leg stays simulated.
"""

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
