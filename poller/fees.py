from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Iterable, Mapping

ZERO = Decimal("0")


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class FeeRule:
    formula: str
    rate: Decimal
    round_up_to: Decimal

    def __post_init__(self):
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))
        object.__setattr__(self, "round_up_to", _decimal(self.round_up_to, "round_up_to"))
        if self.formula not in ("quadratic", "notional"):
            raise ValueError("Unsupported fee formula")
        if self.rate < 0 or self.round_up_to <= 0:
            raise ValueError("rate must be nonnegative and round_up_to must be positive")

    def unrounded(self, price, quantity):
        price = _decimal(price, "price")
        quantity = _decimal(quantity, "quantity")
        if not ZERO <= price <= 1 or quantity < 0:
            raise ValueError("price must be in [0, 1] and quantity must be nonnegative")
        amount = self.rate * quantity * price
        return amount * (1 - price) if self.formula == "quadratic" else amount

    def round_fee(self, amount):
        return (amount / self.round_up_to).to_integral_value(rounding=ROUND_CEILING) * self.round_up_to


class ConfiguredFeeModel:

    def __init__(self, rules: Mapping[str, FeeRule]):
        if set(rules) != {"kalshi", "polymarket"} or not all(
                isinstance(rule, FeeRule) for rule in rules.values()):
            raise ValueError("Explicit FeeRules required for kalshi and polymarket")
        self.rules = dict(rules)

    @classmethod
    def from_config(cls, config):
        try:
            return cls({exchange: FeeRule(**config[exchange])
                        for exchange in ("kalshi", "polymarket")})
        except (KeyError, TypeError) as exc:
            raise ValueError("Both exchanges need formula, rate, and round_up_to") from exc

    def __call__(self, exchange: str, price: Decimal, quantity: Decimal) -> Decimal:
        rule = self.rules[exchange]
        return rule.round_fee(rule.unrounded(price, quantity))

    def estimate_fills(self, exchange: str, fills: Iterable) -> Decimal:
        rule = self.rules[exchange]
        amount = sum((rule.unrounded(fill.price, fill.size) for fill in fills), ZERO)
        return rule.round_fee(amount)


def load_fee_model(path):
    import json
    from pathlib import Path

    return ConfiguredFeeModel.from_config(json.loads(Path(path).read_text()))
