from decimal import Decimal as D

import pytest

from poller.fees import ConfiguredFeeModel, FeeRule
from poller.orderbook import AskLevel, OrderBook
from poller.sizing import size_direction


@pytest.fixture
def model():
    return ConfiguredFeeModel.from_config({
        "kalshi": {"formula": "quadratic", "rate": "0.07", "round_up_to": "0.01"},
        "polymarket": {"formula": "notional", "rate": "0", "round_up_to": "0.0001"},
    })


def test_small_order_rounds_up(model):
    assert model("kalshi", D("0.41"), D("1")) == D("0.02")
    assert model("kalshi", D("0.5"), D("100")) == D("1.75")
    assert model("polymarket", D("0.55"), D("120")) == 0
    assert model("kalshi", D("0.5"), D("0")) == 0


def test_brief_fee_rounds_after_aggregating_fills(model):
    fills = [AskLevel(D("0.41"), D("80")), AskLevel(D("0.42"), D("40"))]
    assert model.estimate_fills("kalshi", fills) == D("2.04")
    assert sum(model("kalshi", f.price, f.size) for f in fills) == D("2.05")


def test_sizing_uses_aggregated_fees(model):
    def book(exchange, outcome, levels):
        return OrderBook(exchange, "example", exchange, outcome,
                         tuple(AskLevel(D(p), D(q)) for p, q in levels), "2026-11-15T00:00:00Z")
    result = size_direction(
        book("kalshi", "YES", [("0.41", "80"), ("0.42", "150")]),
        book("polymarket", "NO", [("0.55", "120"), ("0.56", "300")]),
        model, max_contracts=120)
    assert result.best_trade.quantity == 120
    assert result.best_trade.fees == D("2.04")
    assert result.best_trade.estimated_profit == D("2.36")


@pytest.mark.parametrize("kwargs", [
    {"formula": "unknown", "rate": "0", "round_up_to": "0.01"},
    {"formula": "quadratic", "rate": "NaN", "round_up_to": "0.01"},
    {"formula": "quadratic", "rate": "-1", "round_up_to": "0.01"},
    {"formula": "quadratic", "rate": "0.07", "round_up_to": "0"},
])
def test_invalid_rules_rejected(kwargs):
    with pytest.raises(ValueError):
        FeeRule(**kwargs)


def test_missing_exchange_rejected():
    with pytest.raises(ValueError):
        ConfiguredFeeModel.from_config({})


@pytest.mark.parametrize("price, quantity", [("NaN", "1"), ("1.1", "1"), ("0.5", "-1")])
def test_invalid_inputs_rejected(model, price, quantity):
    with pytest.raises(ValueError):
        model("kalshi", D(price), D(quantity))


def test_load_fee_model(tmp_path):
    import json
    from poller.fees import load_fee_model
    path = tmp_path / "fees.json"
    path.write_text(json.dumps({
        "kalshi": {"formula": "quadratic", "rate": "0.07", "round_up_to": "0.01"},
        "polymarket": {"formula": "notional", "rate": "0", "round_up_to": "0.0001"},
    }))
    assert load_fee_model(path)("kalshi", D("0.5"), D("100")) == D("1.75")


def test_live_runner_requires_config_before_collecting():
    from poller.run import main
    with pytest.raises(SystemExit) as exc:
        main(["--once"])
    assert exc.value.code == 2
