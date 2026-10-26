import json
from pathlib import Path

PAIRS_PATH = Path(__file__).resolve().parent / "pairs.json"


def load_pairs(path=PAIRS_PATH, active_only=True):
    """Return the list of pairs from pairs.json.

    active_only=True skips pairs with "active": false, which is how a pair
    gets paused without deleting it.
    """
    with open(path, encoding="utf-8") as f:
        pairs = json.load(f)
    if active_only:
        pairs = [p for p in pairs if p.get("active", True)]
    return pairs
