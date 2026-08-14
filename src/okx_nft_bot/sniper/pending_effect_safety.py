from __future__ import annotations

from functools import wraps
import threading
from typing import Any

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


def _pending_result(order_id: str, *, detail: str = "pending confirmation") -> dict[str, Any]:
    return {
        "success": False,
        "error": f"Order {order_id} {detail}",
        "pending": True,
    }


def install_pending_effect_safety(buyer_cls: type) -> None:
    """Prevent duplicate execution attempts while an order's effect is ambiguous.

    R61 supplied the in-process latch. R75 extends the same fail-closed boundary
    across process restarts with a durable SQLite claim keyed by
    ``(wallet, chain, order_id)``. The guard still performs no network readback,
    retry, submit, cancel, or reconciliation of its own.
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
                return _pending_result(order_id)

            db_path = getattr(self, "execution_db_path", None)
            wallet = str(getattr(self, "buyer_address", "") or "").strip()
            if db_path is None or not wallet:
                return _pending_result(order_id, detail="blocked: durable effect identity unavailable")

            try:
                durable = DurablePendingEffectStore(db_path)
                reserved = durable.reserve(wallet=wallet, chain=chain, order_id=order_id)
            except Exception as exc:
                return _pending_result(
                    order_id,
                    detail=f"blocked: durable effect claim unavailable ({type(exc).__name__})",
                )

            if not reserved:
                pending_orders.add(order_id)
                return _pending_result(order_id)
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
            # Unknown interruption: retain both in-memory and durable claims.
            raise

        keep_reserved = bool(isinstance(result, dict) and result.get("pending") is True)
        failed_orders = getattr(self, "_failed_orders", set())
        if order_id in failed_orders:
            keep_reserved = True

        if keep_reserved:
            try:
                durable.mark_pending(
                    wallet=wallet,
                    chain=chain,
                    order_id=order_id,
                    tx_hash=result.get("tx_hash") if isinstance(result, dict) else None,
                )
            except Exception:
                # The pre-effect reservation already exists. Failure to enrich it
                # must never make the ambiguous effect retryable.
                pass
        else:
            try:
                durable.release(wallet=wallet, chain=chain, order_id=order_id)
            except Exception:
                # A stale durable claim is safer than an unsafe duplicate submit.
                pass
            with lock:
                getattr(self, "_pending_orders", set()).discard(order_id)

        return result

    guarded_execute_buy._r61_pending_effect_guard = True
    guarded_execute_buy._r75_durable_pending_effect_guard = True
    buyer_cls._execute_buy = guarded_execute_buy
