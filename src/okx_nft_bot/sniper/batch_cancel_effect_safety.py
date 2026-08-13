from __future__ import annotations

import logging
from functools import wraps
from typing import Any


log = logging.getLogger("sniper.batch_cancel_effect_safety")

_FLAG = "_r60_batch_cancel_unconfirmed"


class _BatchCancelEffectProxy:
    """Prevent a second cancel effect after an unconfirmed batch cancel.

    CounterBidder first tries ``cancel_all_via_counter()`` when two or more
    offers are live.  The underlying OKX client collapses both deterministic
    pre-broadcast failures and ambiguous post-broadcast receipt failures into a
    false result.  Legacy CounterBidder then immediately falls back to
    per-order ``cancel_offer()`` calls.

    Because an ``incrementCounter()`` transaction may already have been
    accepted when its receipt is lost, that fallback can cross a second effect
    boundary.  R60 records an unconfirmed batch result for the current
    ``_cancel_existing_offer`` operation and suppresses only that per-order
    fallback.  It adds no retry, readback, marketplace request, or on-chain
    action of its own.
    """

    __slots__ = ("_client", "_owner")

    def __init__(self, client: Any, owner: Any) -> None:
        self._client = client
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def cancel_all_via_counter(self, *args: Any, **kwargs: Any) -> bool:
        try:
            result = self._client.cancel_all_via_counter(*args, **kwargs)
        except Exception:
            setattr(self._owner, _FLAG, True)
            raise

        if result is True:
            return True

        setattr(self._owner, _FLAG, True)
        log.warning(
            "R60: batch cancel not exactly confirmed; suppressing per-order fallback"
        )
        return False

    def cancel_offer(self, *args: Any, **kwargs: Any) -> Any:
        if getattr(self._owner, _FLAG, False):
            log.warning(
                "R60: per-order cancel fallback suppressed after unconfirmed batch cancel"
            )
            return False
        return self._client.cancel_offer(*args, **kwargs)


def install_batch_cancel_effect_safety(bidder_class: type[Any]) -> None:
    """Fail closed across CounterBidder's batch-to-per-order cancel boundary.

    The guard wraps the already-hardened R58/R59 client accessor.  Its flag is
    reset at entry and in ``finally`` around exactly one
    ``_cancel_existing_offer`` invocation, so suppression cannot leak into a
    later or unrelated cancel operation.
    """
    current_cancel = bidder_class._cancel_existing_offer
    if getattr(current_cancel, "_r60_batch_cancel_effect_guard", False):
        return

    current_get = bidder_class._get_okx_client

    @wraps(current_get)
    def guarded_get(self: Any) -> Any:
        client = current_get(self)
        if client is None:
            return None
        if isinstance(client, _BatchCancelEffectProxy) and client._owner is self:
            return client
        return _BatchCancelEffectProxy(client, self)

    @wraps(current_cancel)
    def guarded_cancel_existing(self: Any, *args: Any, **kwargs: Any) -> Any:
        setattr(self, _FLAG, False)
        try:
            return current_cancel(self, *args, **kwargs)
        finally:
            setattr(self, _FLAG, False)

    guarded_get._r60_batch_cancel_effect_proxy = True
    guarded_cancel_existing._r60_batch_cancel_effect_guard = True
    bidder_class._get_okx_client = guarded_get
    bidder_class._cancel_existing_offer = guarded_cancel_existing
