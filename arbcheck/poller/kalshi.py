"""Kalshi poller. Owner: ______

Week 1 goal: for every active pair, fetch the Kalshi order book and save the raw
response with a timestamp to data/raw/ BEFORE parsing anything.

What you read from pairs.json:  pair["kalshi_ticker"]
Get the pairs with:             from pairs import load_pairs

Public base URL (no key needed for market data):
    https://api.elections.kalshi.com/trade-api/v2

Remember Kalshi only publishes bids. A NO bid at 0.59 is a YES ask at 0.41.
That conversion is week 2 work, so just save the raw response for now.
"""

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
