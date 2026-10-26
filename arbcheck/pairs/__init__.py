"""Shared access to the matched pairs list.

Everyone reads pairs through load_pairs() so the file only gets parsed one way.

    from pairs import load_pairs
    for pair in load_pairs():
        print(pair["kalshi_ticker"], pair["poly_yes_token"])
"""

from pairs.loader import PAIRS_PATH, load_pairs

__all__ = ["PAIRS_PATH", "load_pairs"]
