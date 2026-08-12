from __future__ import annotations

from functools import wraps
from typing import Any, Mapping

_PLACEHOLDER_IDS = frozenset({"", "?", "pending", "none", "null"})
_RECEIPT_KEYS = ("orderId", "offerId", "orderHash", "id", "order_id", "offer_id")


def _durable_id(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if candidate.lower() in _PLACEHOLDER_IDS:
        return None
    return candidate


def _submit_receipt_id(response: Mapping[str, Any]) -> str | None:
    """Return a durable order ID for a success-shaped submit response.

    Nonzero OKX codes and explicit ``data.errors`` are intentionally returned as
    ``None`` without raising here so the existing two-step caller can preserve
    its retry/rejection behavior. A response that otherwise looks successful
    must contain a durable exchange order identifier.
    """
    code = str(response.get("code", ""))
    if code not in {"", "0"}:
        return None

    data = response.get("data")
    if isinstance(data, Mapping):
        errors = data.get("errors")
        if isinstance(errors, (list, tuple)) and errors:
            return None

        success_ids = data.get("successOrderIds")
        if isinstance(success_ids, (list, tuple)):
            for value in success_ids:
                order_id = _durable_id(value)
                if order_id:
                    return order_id

        for key in _RECEIPT_KEYS:
            order_id = _durable_id(data.get(key))
            if order_id:
                return order_id

    for key in _RECEIPT_KEYS:
        order_id = _durable_id(response.get(key))
        if order_id:
            return order_id

    message = str(response.get("msg") or "").strip()
    suffix = f": {message}" if message else ""
    raise RuntimeError(f"success response missing durable order id{suffix}")


def install_receipt_safety(client_class: type[Any]) -> None:
    """Require a durable receipt after the exact OKX submitOrder HTTP effect."""
    original_request = client_class._request
    if getattr(original_request, "_r25_receipt_guard", False):
        return

    @wraps(original_request)
    def guarded_request(
        self: Any,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = original_request(
            self,
            method=method,
            path=path,
            params=params,
            payload=payload,
        )

        canonical_path = str(path or "").split("?", 1)[0]
        is_submit = (
            str(method or "").upper() == "POST"
            and canonical_path == self._SUBMIT_ORDER_PATH
        )
        if not is_submit:
            return response

        if not isinstance(response, Mapping):
            from okx_nft_bot.counterbid.okx_api import OKXSubmitError

            raise OKXSubmitError(
                "submitOrder receipt gate blocked: response is not an object"
            )

        code = str(response.get("code", ""))
        data = response.get("data")
        explicit_errors = (
            isinstance(data, Mapping)
            and isinstance(data.get("errors"), (list, tuple))
            and bool(data.get("errors"))
        )
        if code not in {"", "0"} or explicit_errors:
            # Let _complete_two_step_offer preserve existing error/retry logic.
            return response

        from okx_nft_bot.counterbid.okx_api import OKXSubmitError

        try:
            _submit_receipt_id(response)
        except Exception as exc:
            raise OKXSubmitError(
                f"submitOrder receipt gate blocked: {exc}"
            ) from exc
        return response

    guarded_request._r25_receipt_guard = True
    client_class._request = guarded_request
