import copy
import json
import logging
from collections import Counter
from datetime import datetime, timezone

from pairs.validate import validate
from poller.fees import reviewed_pair_fees
from poller.opportunities import CycleChecker

logger = logging.getLogger(__name__)


class LiveCycleChecker:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.states = {}
        self.pending = []
        self.just_opened = []
        self.suppressed = {}

    @staticmethod
    def _entries(entries, pair):
        for entry in entries:
            entry.update(kalshi_rules_url=pair["kalshi_rules_url"],
                         poly_rules_url=pair["poly_rules_url"],
                         rules_verified=True,
                         fee_config=copy.deepcopy(pair["fee_config"]))
        return entries

    def configure(self, pairs):
        self.suppressed = {}
        eligible = {}
        counts = Counter(p.get("id") for p in pairs
                         if isinstance(p, dict) and isinstance(p.get("id"), str))
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            pid = pair.get("id")
            if not isinstance(pid, str):
                logger.warning("Skipping pair with invalid id")
                continue
            try:
                errors, _ = validate([pair])
                if errors:
                    raise ValueError("; ".join(errors))
                if counts[pid] != 1:
                    raise ValueError("duplicate pair id")
                if pair.get("active") is not True:
                    raise ValueError("pair inactive")
                model = reviewed_pair_fees(pair)
                fingerprint = json.dumps(pair, sort_keys=True)
                eligible[pid] = (fingerprint, copy.deepcopy(pair), model)
            except (ValueError, TypeError, KeyError) as exc:
                self.suppressed[pid] = str(exc)
                logger.warning("Suppressing live plans for %s: %s", pid, exc)

        for pid in list(self.states):
            old = self.states[pid]
            if pid not in eligible or old[0] != eligible[pid][0]:
                entries = old[2].tracker.close_missing(set())
                for entry in entries:
                    entry["end_reason"] = "configuration_changed" if pid in eligible else "ineligible"
                self.pending += self._entries(entries, old[1])
                del self.states[pid]
        for pid, (fingerprint, pair, model) in eligible.items():
            if pid not in self.states:
                self.states[pid] = (fingerprint, pair, CycleChecker(fee_model=model))

    def check_cycle(self, books):
        at = self.clock() 
        self.just_opened = []
        closed, self.pending = self.pending, []
        grouped = {}
        for book in books:
            grouped.setdefault(book.pair_id, []).append(book)
        for pid, (_, pair, checker) in self.states.items():
            expected = {
                ("kalshi", "YES"): pair["kalshi_ticker"],
                ("kalshi", "NO"): pair["kalshi_ticker"],
                ("polymarket", "YES"): pair["poly_yes_token"],
                ("polymarket", "NO"): pair["poly_no_token"],
            }
            current = grouped.get(pid, [])
            keys = [(book.exchange, book.outcome) for book in current]
            if len(set(keys)) != len(keys) or any(
                    expected.get((b.exchange, b.outcome)) != b.market for b in current):
                logger.warning("Suppressing live plans for %s: conflicting book identity", pid)
                current = []
            candidate = copy.deepcopy(checker)
            candidate.latest.clear()
            try:
                entries = candidate.check_cycle(current, at=at)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                logger.exception("Invalid live books for %s", pid)
                closed += self._entries(checker.tracker.close_missing(set()), pair)
                checker.latest.clear()
                checker.just_opened = []
            else:
                self.states[pid] = (self.states[pid][0], pair, candidate)
                closed += self._entries(entries, pair)
                self.just_opened += candidate.just_opened
        return closed

    def finish(self):
        entries, self.pending = self.pending, []
        for _, pair, checker in self.states.values():
            entries += self._entries(checker.finish(), pair)
        return entries
