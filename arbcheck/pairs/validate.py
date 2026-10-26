"""Check pairs.json for mistakes before they reach the poller.

Run from the repo root:
    python -m pairs.validate

Errors fail the check (CI blocks the PR). Warnings are things a human should
look at but that won't break anything.
"""

import json
import re
import sys
from datetime import date

from pairs.loader import PAIRS_PATH

REQUIRED_FIELDS = {
    "id": str,
    "name": str,
    "category": str,
    "kalshi_ticker": str,
    "poly_slug": str,
    "poly_yes_token": str,
    "poly_no_token": str,
    "resolution_date": str,
    "kalshi_rules_url": str,
    "poly_rules_url": str,
    "rule_notes": str,
    "active": bool,
}

CATEGORIES = {"sports", "economics", "politics", "crypto", "weather", "culture", "other"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate(pairs, today=None):
    """Return (errors, warnings) as two lists of strings."""
    today = today or date.today()
    errors, warnings = [], []

    if not isinstance(pairs, list):
        return ["pairs.json must be a JSON list of pair objects"], []

    seen_ids, seen_kalshi = set(), set()

    for i, pair in enumerate(pairs):
        label = f"pair #{i + 1}"
        if not isinstance(pair, dict):
            errors.append(f"{label}: must be an object")
            continue
        label = f"pair #{i + 1} ({pair.get('id', 'no id')})"

        for field, kind in REQUIRED_FIELDS.items():
            if field not in pair:
                errors.append(f"{label}: missing '{field}'")
            elif not isinstance(pair[field], kind):
                errors.append(f"{label}: '{field}' should be {kind.__name__}")
            elif kind is str and field != "rule_notes" and not pair[field].strip():
                errors.append(f"{label}: '{field}' is empty")

        extra = set(pair) - set(REQUIRED_FIELDS)
        if extra:
            warnings.append(f"{label}: unknown fields {sorted(extra)} (typo?)")

        pid = pair.get("id")
        if isinstance(pid, str) and pid:
            if not ID_PATTERN.match(pid):
                errors.append(f"{label}: id should be lowercase-with-dashes")
            if pid in seen_ids:
                errors.append(f"{label}: duplicate id")
            seen_ids.add(pid)

        ticker = pair.get("kalshi_ticker")
        if isinstance(ticker, str) and ticker:
            if ticker != ticker.upper():
                errors.append(f"{label}: kalshi_ticker should be uppercase")
            if ticker in seen_kalshi:
                warnings.append(f"{label}: kalshi_ticker used by another pair")
            seen_kalshi.add(ticker)

        yes_tok, no_tok = pair.get("poly_yes_token"), pair.get("poly_no_token")
        for name, tok in (("poly_yes_token", yes_tok), ("poly_no_token", no_tok)):
            if isinstance(tok, str) and tok and not tok.isdigit():
                errors.append(f"{label}: {name} should be all digits")
        if isinstance(yes_tok, str) and yes_tok and yes_tok == no_tok:
            errors.append(f"{label}: YES and NO tokens are the same")

        res = pair.get("resolution_date")
        if isinstance(res, str) and res:
            try:
                res_date = date.fromisoformat(res)
                if res_date < today and pair.get("active", True):
                    warnings.append(f"{label}: resolved {res} but still active")
            except ValueError:
                errors.append(f"{label}: resolution_date should be YYYY-MM-DD")

        for field in ("kalshi_rules_url", "poly_rules_url"):
            url = pair.get(field)
            if isinstance(url, str) and url and not url.startswith("https://"):
                errors.append(f"{label}: {field} should start with https://")

        cat = pair.get("category")
        if isinstance(cat, str) and cat and cat not in CATEGORIES:
            warnings.append(f"{label}: category '{cat}' not in {sorted(CATEGORIES)}")

        notes = pair.get("rule_notes")
        if isinstance(notes, str) and not notes.strip():
            warnings.append(f"{label}: rule_notes is empty")

    return errors, warnings


def main(path=PAIRS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            pairs = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: pairs.json is not valid JSON ({e})")
        return 1

    errors, warnings = validate(pairs)
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")

    active = sum(1 for p in pairs if isinstance(p, dict) and p.get("active", True))
    print(f"{len(pairs)} pairs ({active} active), {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
