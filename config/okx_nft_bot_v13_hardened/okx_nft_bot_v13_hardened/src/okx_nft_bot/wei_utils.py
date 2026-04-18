"""
Precision-safe wei conversion helpers.

float * 10**18 loses precision (e.g. 0.1 * 1e18 → 99999999999999998).
We convert through Decimal to avoid this.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

_WEI = Decimal(10) ** 18
_GWEI = Decimal(10) ** 9
_USDT_DECIMALS = Decimal(10) ** 6  # USDT / USDC on many chains


def to_wei(amount: float | str | Decimal, decimals: int = 18) -> int:
    """Convert a human-readable amount to the smallest unit (wei / satoshi / etc.)

    Examples:
        to_wei(0.1)           → 100000000000000000
        to_wei(0.1, 6)        → 100000  (USDT/USDC)
        to_wei("0.123456789") → 123456789000000000
    """
    d = Decimal(str(amount))
    factor = Decimal(10) ** decimals
    return int(d * factor)


def to_gwei(amount: float | str | Decimal) -> int:
    """Convert gwei amount to wei (9 decimals)."""
    return to_wei(amount, decimals=9)


def from_wei(wei_amount: int, decimals: int = 18) -> Decimal:
    """Convert from smallest unit back to human-readable Decimal."""
    return Decimal(wei_amount) / (Decimal(10) ** decimals)
