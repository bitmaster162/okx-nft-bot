from __future__ import annotations

from functools import wraps
import threading
from typing import Any


def install_pending_effect_safety(buyer_cls: type) -> None:
    """Prevent duplicate execution attempts while an order's effect is ambiguous.

    The guard is deliberately local and fail-closed: it does not perform any
    network readback, retry, submit, cancel, or reconciliation. A receipt timeout
    (or an exception the buyer already classifies as failed/uncertain) keeps the
    order reserved in memory so the same stale listing cannot be broadcast again
    by another call in the same process.
    """
    original = buyer_cls._execute_buy
    if getattr(original, "_r61_pending_effect_guard", False):
        return

    @wraps(original)
    def guarded_execute_buy(
        self,
        listing: dict,
        chain: str,
        price: float,
        *,
        collection_address: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        order_id = str(listing.get("orderId") or listing.get("orderHash") or "")
        if not order_id:
            return original(
                self,
                listing,
                chain,
                price,
                collection_address=collection_address,
                currency=currency,
            )

        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._lock = lock

        with lock:
            pending_orders = getattr(self, "_pending_orders", None)
            if pending_orders is None:
                pending_orders = set()
                self._pending_orders = pending_orders
            if order_id in pending_orders:
                return {
                    "success": False,
                    "error": f"Order {order_id} pending confirmation",
                    "pending": True,
                }
            pending_orders.add(order_id)

        try:
            result = original(
                self,
                listing,
                chain,
                price,
                collection_address=collection_address,
                currency=currency,
            )
        except BaseException:
            # Unknown interruption: retain the reservation. Releasing it without
            # knowing whether broadcast was attempted could permit a duplicate.
            raise

        keep_reserved = bool(isinstance(result, dict) and result.get("pending") is True)
        failed_orders = getattr(self, "_failed_orders", set())
        if order_id in failed_orders:
            keep_reserved = True

        if not keep_reserved:
            with lock:
                getattr(self, "_pending_orders", set()).discard(order_id)

        return result

    guarded_execute_buy._r61_pending_effect_guard = True
    buyer_cls._execute_buy = guarded_execute_buy
