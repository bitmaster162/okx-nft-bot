from __future__ import annotations

from functools import wraps
from typing import Any, Mapping


def _is_literal_token_zero(value: Any) -> bool:
    """Return True only for numeric/string token id zero, never bool False."""
    if isinstance(value, bool):
        return False
    return value == 0 or value == "0"


def _normalize_sell_read_response(response: Any) -> Any:
    """Canonicalize literal token #0 to string ``"0"`` in OKX sell reads.

    CounterBidder's legacy sell phase uses truthiness to decide whether an owned
    NFT has a token id, and compares listing token ids against inventory token
    ids without type coercion.  Numeric zero therefore disappears from inventory
    while an existing numeric-zero listing can compare unequal to string zero.

    Only the nested ``data.data`` item list returned by ``get_wallet_nfts`` and
    ``get_listings`` is adapted.  The original response is not mutated.
    """
    if not isinstance(response, Mapping):
        return response

    data = response.get("data")
    if not isinstance(data, Mapping):
        return response

    items = data.get("data")
    if not isinstance(items, list):
        return response

    changed = False
    normalized_items: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and _is_literal_token_zero(item.get("tokenId")):
            normalized = dict(item)
            normalized["tokenId"] = "0"
            normalized_items.append(normalized)
            changed = True
        else:
            normalized_items.append(item)

    if not changed:
        return response

    normalized_data = dict(data)
    normalized_data["data"] = normalized_items
    normalized_response = dict(response)
    normalized_response["data"] = normalized_data
    return normalized_response


class _SellReadTokenZeroProxy:
    """Delegate the OKX client while canonicalizing sell-side read token ids."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def get_wallet_nfts(self, *args: Any, **kwargs: Any) -> Any:
        return _normalize_sell_read_response(
            self._client.get_wallet_nfts(*args, **kwargs)
        )

    def get_listings(self, *args: Any, **kwargs: Any) -> Any:
        return _normalize_sell_read_response(
            self._client.get_listings(*args, **kwargs)
        )


def install_sell_token_zero_safety(bidder_class: type[Any]) -> None:
    """Preserve token #0 through CounterBidder's sell-side read boundary.

    The 240KB legacy CounterBidder is deliberately left untouched.  Its OKX
    client accessor is wrapped with a transparent proxy that changes only the
    two read-only inputs consumed by ``_run_sell_phase``.  All effectful client
    methods (cancel/list/create/etc.) are delegated unchanged.
    """
    current = bidder_class._get_okx_client
    if getattr(current, "_r58_sell_token_zero_scope", False):
        return

    original = current

    @wraps(original)
    def guarded(self: Any) -> Any:
        client = original(self)
        if isinstance(client, _SellReadTokenZeroProxy):
            return client
        return _SellReadTokenZeroProxy(client)

    guarded._r58_sell_token_zero_scope = True
    bidder_class._get_okx_client = guarded
