"""Placeholder basic sample fee model for sizing engine testing"""

from decimal import Decimal


def sample_fee(exchange: str, price: Decimal, quantity: Decimal) -> Decimal:
    return price * quantity * Decimal("0.01")
