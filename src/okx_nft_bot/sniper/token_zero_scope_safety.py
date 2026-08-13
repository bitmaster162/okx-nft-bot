from __future__ import annotations

from functools import wraps
from typing import Any


def _is_literal_token_zero(value: Any) -> bool:
    """Return True only for numeric/string token id zero, never bool False."""
    if isinstance(value, bool):
        return False
    return value == 0 or value == "0"


def install_token_zero_scope_safety(bidder_class: type[Any]) -> None:
    """Preserve OKX token #0 as item scope in CounterBidder raw parsing.

    R56 is deliberately narrow: the legacy parser is allowed to perform all
    existing address, price, currency, maker, and order-id normalization.  Only
    the falsey token-id classification is repaired after that parser returns.
    """
    current = bidder_class._raw_to_rival
    if getattr(current, "_r56_token_zero_scope", False):
        return

    original = current

    @wraps(original)
    def guarded(self: Any, raw: dict[str, Any], chain: str) -> Any:
        result = original(self, raw, chain)
        if _is_literal_token_zero(raw.get("tokenId")):
            result.token_id = "0"
            result.source_type = "token_offer"
        return result

    guarded._r56_token_zero_scope = True
    bidder_class._raw_to_rival = guarded
