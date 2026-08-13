from __future__ import annotations

from functools import wraps
from typing import Any


class SellCancelNotConfirmed(RuntimeError):
    """Raised when a sell-side listing cancel did not return exact confirmation."""


class _SellCancelConfirmationProxy:
    """Delegate the OKX client while making sell re-list cancellation fail closed.

    CounterBidder's legacy sell phase already treats an exception from
    ``cancel_listing`` as an instruction to ``continue`` without discarding the
    existing token from ``our_listed_token_ids``.  It did not, however, inspect
    the boolean return value.  A deterministic ``False`` therefore flowed into
    the re-list path and could create a second listing while the old one was
    still active.

    R59 changes no marketplace request and adds no retry.  It only converts any
    non-``True`` cancel result into an exception at the CounterBidder client
    boundary so the existing fail-closed control flow is actually exercised.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def cancel_listing(self, *args: Any, **kwargs: Any) -> bool:
        result = self._client.cancel_listing(*args, **kwargs)
        if result is not True:
            order_id = ""
            if args:
                order_id = str(args[0])
            elif "order_id" in kwargs:
                order_id = str(kwargs["order_id"])
            prefix = f" for {order_id[:14]}" if order_id else ""
            raise SellCancelNotConfirmed(
                f"sell cancel not confirmed{prefix}: result={result!r}"
            )
        return True


def install_sell_cancel_confirmation_safety(bidder_class: type[Any]) -> None:
    """Require exact cancel confirmation before CounterBidder can re-list.

    The installer wraps the already-hardened ``_get_okx_client`` accessor, so it
    composes with R58 token-zero read normalization.  Only ``cancel_listing`` is
    specialized; every other read/effect method is delegated unchanged.
    """
    current = bidder_class._get_okx_client
    if getattr(current, "_r59_sell_cancel_confirmation", False):
        return

    original = current

    @wraps(original)
    def guarded(self: Any) -> Any:
        client = original(self)
        if client is None or isinstance(client, _SellCancelConfirmationProxy):
            return client
        return _SellCancelConfirmationProxy(client)

    guarded._r59_sell_cancel_confirmation = True
    bidder_class._get_okx_client = guarded
