
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import json
from pathlib import Path

ZERO = Decimal("0")


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Expected a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite decimal")
    return result


@dataclass(frozen=True)
class FeeRule:
    formula: str
    rate: Decimal
    round_up_to: Decimal | None = None
    rounding: str = "ceiling"
    balance_precision: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "rate", _decimal(self.rate))
        if self.rounding not in ("ceiling", "kalshi", "polymarket"):
            raise ValueError("Unsupported rounding policy")
        if self.rounding == "ceiling":
            if self.round_up_to is None:
                raise ValueError("Ceiling rounding needs round_up_to")
            object.__setattr__(self, "round_up_to", _decimal(self.round_up_to))
            if self.round_up_to <= 0:
                raise ValueError("Rounding increment must be positive")
        elif self.round_up_to is not None:
            raise ValueError("round_up_to only applies to ceiling rounding")
        if self.rounding == "kalshi":
            if self.balance_precision is None:
                raise ValueError("Kalshi requires explicit member balance_precision")
            object.__setattr__(self, "balance_precision", _decimal(self.balance_precision))
            if self.balance_precision not in (Decimal("0.0001"), Decimal("0.01")):
                raise ValueError("Unsupported Kalshi member balance precision")
        elif self.balance_precision is not None:
            raise ValueError("balance_precision only applies to Kalshi")
        if self.formula not in ("quadratic", "notional"):
            raise ValueError("Unsupported fee formula")
        if self.rate < 0:
            raise ValueError("Rate must be nonnegative")

    def unrounded(self, price, quantity):
        price, quantity = _decimal(price), _decimal(quantity)
        if not 0 <= price <= 1 or quantity < 0:
            raise ValueError("Invalid price or quantity")
        amount = self.rate * quantity * price
        return amount * (1 - price) if self.formula == "quadratic" else amount

    def round_fee(self, amount):
        if self.rounding != "ceiling":
            raise ValueError("Use fill estimation for exchange rounding")
        return (amount / self.round_up_to).to_integral_value(rounding=ROUND_CEILING) * self.round_up_to


class ConfiguredFeeModel:
    def __init__(self, rules):
        if set(rules) != {"kalshi", "polymarket"} or not all(
                isinstance(rule, FeeRule) for rule in rules.values()):
            raise ValueError("Explicit rules required for both exchanges")
        if rules["kalshi"].rounding == "polymarket" or rules["polymarket"].rounding == "kalshi":
            raise ValueError("Rounding policy does not match exchange")
        self.rules = dict(rules)

    @classmethod
    def from_config(cls, config):
        try:
            return cls({exchange: FeeRule(**config[exchange])
                        for exchange in ("kalshi", "polymarket")})
        except (KeyError, TypeError) as exc:
            raise ValueError("Both exchanges need valid formula, rate, and rounding parameters") from exc

    def __call__(self, exchange, price, quantity):
        from poller.orderbook import AskLevel
        return self.estimate_fills(exchange, [AskLevel(_decimal(price), _decimal(quantity))])

    def estimate_fills(self, exchange, fills):
        rule = self.rules[exchange]
        total = accumulator = ZERO
        for fill in fills:
            price, quantity = _decimal(fill.price), _decimal(fill.size)
            amount = rule.unrounded(price, quantity)
            if quantity == 0:
                continue
            if rule.rounding == "ceiling":
                total += amount
            elif rule.rounding == "polymarket":
                total += amount.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
            else:
                cost = price * quantity
                if cost != cost.quantize(Decimal("0.000001")):
                    raise ValueError("Kalshi fill cost exceeds six-decimal precision")
                trade_fee = amount.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
                grid = rule.balance_precision
                revenue = -cost
                aligned = ((revenue - trade_fee) / grid).to_integral_value(rounding=ROUND_FLOOR) * grid
                rounding_fee = revenue - trade_fee - aligned
                accumulator += rounding_fee
                rebate = min((accumulator / grid).to_integral_value(rounding=ROUND_FLOOR),
                             ((trade_fee + rounding_fee) / grid).to_integral_value(rounding=ROUND_FLOOR)) * grid
                accumulator -= rebate
                total += trade_fee + rounding_fee - rebate
        return rule.round_fee(total) if rule.rounding == "ceiling" else total


def load_fee_model(path):
    return ConfiguredFeeModel.from_config(json.loads(Path(path).read_text()))


def reviewed_pair_fees(pair):
    if pair.get("rules_verified") is not True:
        raise ValueError("pair rules are unverified")
    config = pair.get("fee_config")
    if not isinstance(config, dict) or config.get("verified") is not True:
        raise ValueError("fees are unverified")
    if not isinstance(config.get("source"), str) or not config["source"].strip():
        raise ValueError("fee review needs source evidence")
    model = ConfiguredFeeModel.from_config(config)
    if model.rules["kalshi"].rounding != "kalshi" or model.rules["polymarket"].rounding != "polymarket":
        raise ValueError("Live fees require exchange-specific rounding policies")
    return model
