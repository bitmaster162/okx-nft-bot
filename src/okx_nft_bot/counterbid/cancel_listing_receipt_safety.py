from __future__ import annotations

from functools import wraps
import logging
from typing import Any, Mapping


log = logging.getLogger("counterbid.cancel_listing_receipt_safety")

_CANCEL_PATH = "/api/v5/mktplace/nft/markets/cancel-listing"
_ACK_KEYS = ("success", "cancelled", "result")


def _confirmed_ack(client: Any, response: Mapping[str, Any]) -> bool:
    """Return True only when a code=0 cancel receipt contains an explicit affirmative ack."""
    if str(response.get("code", "")) != "0":
        return False

    success = client._extract_scalar(response, keys=_ACK_KEYS)
    if success is None:
        return False
    if isinstance(success, bool):
        return success
    return str(success).lower() not in {"0", "false", "failed"}


def install_cancel_listing_receipt_safety(client_class: type[Any]) -> None:
    """Fail closed when ``cancel_listing`` lacks explicit cancellation acknowledgement.

    R57 already constrains each exact ``cancel-listing`` POST to one transport
    attempt. R62 preserves the legacy orderId -> offerId fallback only after a
    deterministic non-zero app code, but a code=0 response is no longer promoted
    to success merely because ``success/cancelled/result`` is absent.

    No readback, retry, RPC, transaction, or other effect is added here.
    """
    current = client_class.cancel_listing
    if getattr(current, "_r62_cancel_listing_receipt_guard", False):
        return

    @wraps(current)
    def guarded_cancel_listing(self: Any, order_id: str) -> bool:
        response = self._request(
            method="POST",
            path=_CANCEL_PATH,
            payload={"orderId": order_id},
        )
        if not isinstance(response, Mapping):
            log.warning("cancel_listing %s: malformed receipt; retaining listing exposure", str(order_id)[:14])
            return False

        if str(response.get("code", "")) != "0":
            response = self._request(
                method="POST",
                path=_CANCEL_PATH,
                payload={"offerId": order_id},
            )
            if not isinstance(response, Mapping):
                log.warning("cancel_listing %s: malformed fallback receipt; retaining listing exposure", str(order_id)[:14])
                return False

        if not _confirmed_ack(self, response):
            log.warning("cancel_listing %s: cancellation not explicitly acknowledged; retaining listing exposure", str(order_id)[:14])
            return False
        return True

    guarded_cancel_listing._r62_cancel_listing_receipt_guard = True
    client_class.cancel_listing = guarded_cancel_listing
