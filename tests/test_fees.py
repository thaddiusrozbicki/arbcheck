from decimal import Decimal as D

import pytest

from poller.fees import ConfiguredFeeModel, FeeRule
from poller.orderbook import AskLevel


def model(precision="0.01", kalshi_rate="0.07", poly_rate="0.05"):
    return ConfiguredFeeModel.from_config({
        "kalshi": {"formula": "quadratic", "rate": kalshi_rate,
                   "rounding": "kalshi", "balance_precision": precision},
        "polymarket": {"formula": "quadratic", "rate": poly_rate, "rounding": "polymarket"},
    })


def test_kalshi_documented_fcm_example():
    # Cost .055, model fee .00363825 -> fee .005, total debit .06.
    fees = ConfiguredFeeModel({
        "kalshi": FeeRule("notional", D("0.06615"), rounding="kalshi", balance_precision="0.01"),
        "polymarket": FeeRule("quadratic", 0, rounding="polymarket"),
    })
    assert fees("kalshi", D("0.55"), D("0.1")) == D("0.005")


def test_kalshi_order_accumulator_and_reset():
    fees = ConfiguredFeeModel({
        "kalshi": FeeRule("notional", "0.15", rounding="kalshi", balance_precision="0.01"),
        "polymarket": FeeRule("quadratic", 0, rounding="polymarket"),
    })
    # Each fill costs .04 with .006 trade fee and .004 rounding overpayment.
    fill = AskLevel(D("0.04"), D(1))
    assert fees.estimate_fills("kalshi", [fill] * 3) == D("0.02")
    assert sum(fees("kalshi", fill.price, fill.size) for _ in range(3)) == D("0.03")
    assert fees.estimate_fills("kalshi", [fill] * 3) == D("0.02")


def test_kalshi_direct_member_and_brief_precision():
    assert model()("kalshi", D("0.41"), D(1)) == D("0.02")
    assert model("0.0001")("kalshi", D("0.41"), D(1)) == D("0.0170")
    fills = [AskLevel(D("0.41"), D(80)), AskLevel(D("0.42"), D(40))]
    assert model().estimate_fills("kalshi", fills) == D("2.04")
    assert model("0.0001").estimate_fills("kalshi", fills) == D("2.0368")


def test_polymarket_nearest_rounding_and_zero_small_fee():
    fees = model()
    assert fees("polymarket", D("0.5"), D(100)) == D("1.25000")
    assert fees("polymarket", D("0.3"), D(1)) == fees("polymarket", D("0.7"), D(1))
    assert fees("polymarket", D("0.001"), D("0.001")) == 0
    assert fees("polymarket", D("0.41"), D(1)) == D("0.01210")
    # Explicit half-up policy at exact midpoint.
    assert model(poly_rate="0.00002")("polymarket", D("0.5"), D(1)) == D("0.00001")


def test_polymarket_rounds_each_assumed_execution():
    fill = AskLevel(D("0.41"), D(1))
    fees = model()
    assert fees.estimate_fills("polymarket", [fill] * 2) == D("0.02420")
    assert fees("polymarket", D("0.41"), D(2)) == D("0.02419")


@pytest.mark.parametrize("kwargs", [
    {"rounding": "kalshi"},
    {"rounding": "kalshi", "balance_precision": "0.001"},
    {"rounding": "kalshi", "balance_precision": "0.01", "round_up_to": "0.01"},
    {"rounding": "polymarket", "balance_precision": "0.01"},
    {"rounding": "invalid"},
    {"rounding": "ceiling", "round_up_to": "0"},
])
def test_invalid_precision_configuration(kwargs):
    with pytest.raises(ValueError):
        FeeRule("quadratic", "0.07", **kwargs)


@pytest.mark.parametrize("price, size", [("NaN", "1"), ("1.1", "1"), ("0.5", "-1")])
def test_bad_fill_rejected(price, size):
    with pytest.raises(ValueError):
        model()("kalshi", D(price), D(size))


def test_empty_and_zero_quantity_have_no_fee():
    for exchange in ("kalshi", "polymarket"):
        assert model().estimate_fills(exchange, []) == 0
        assert model()(exchange, D("0.55"), D(0)) == 0


def test_live_rejects_old_generic_rounding():
    from poller.fees import reviewed_pair_fees
    with pytest.raises(ValueError, match="exchange-specific"):
        reviewed_pair_fees({"rules_verified": True, "fee_config": {
            "verified": True, "source": "fixture",
            "kalshi": {"formula": "quadratic", "rate": "0.07", "round_up_to": "0.01"},
            "polymarket": {"formula": "quadratic", "rate": "0.05", "round_up_to": "0.00001"},
        }})
