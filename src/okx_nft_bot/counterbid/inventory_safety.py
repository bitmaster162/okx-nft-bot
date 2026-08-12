from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any, Mapping


_STRICT_INVENTORY: ContextVar[bool] = ContextVar(
    "okx_strict_inventory_response_guard",
    default=False,
)
_STRICT_ENDPOINTS = frozenset(
    {
        "/priapi/v1/nft/trading/offer/token/list",
        "/priapi/v1/nft/trading/offer/collection/list",
    }
)


def install_inventory_safety(client_class: type[Any]) -> None:
    """Fail closed on semantic OKX inventory errors in strict inventory mode.

    ``get_my_offers(require_all_endpoints=True)`` is used by safety-critical
    reconciliation/killswitch callers as an authoritative inventory read. The
    legacy implementation catches transport exceptions but silently skips HTTP
    success responses carrying a non-zero OKX application code. This wrapper
    converts those semantic endpoint failures into exceptions only while strict
    inventory mode is active, allowing the existing aggregation logic to report
    all/partial endpoint failure instead of an authoritative empty inventory.
    """
    current_request = client_class._request
    current_get_my_offers = client_class.get_my_offers
    request_installed = bool(
        getattr(current_request, "_r38_strict_inventory_response_guard", False)
    )
    getter_installed = bool(
        getattr(current_get_my_offers, "_r38_strict_inventory_context", False)
    )
    if request_installed and getter_installed:
        return
    if request_installed != getter_installed:
        raise RuntimeError("partial R38 strict inventory installation detected")

    original_request = current_request
    original_get_my_offers = current_get_my_offers

    @wraps(original_request)
    def guarded_request(
        self: Any,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = original_request(
            self,
            method=method,
            path=path,
            params=params,
            payload=payload,
        )
        if not _STRICT_INVENTORY.get():
            return result

        canonical_path = str(path or "").split("?", 1)[0]
        if str(method or "").upper() != "GET" or canonical_path not in _STRICT_ENDPOINTS:
            return result
        if not isinstance(result, Mapping):
            raise RuntimeError(
                f"strict inventory endpoint {canonical_path} returned a non-object response"
            )

        code = str(result.get("code", "0"))
        if code not in {"0", ""}:
            msg = str(result.get("msg") or result.get("message") or "unknown error")
            raise RuntimeError(
                f"strict inventory endpoint {canonical_path} returned code={code} msg={msg}"
            )
        return result

    @wraps(original_get_my_offers)
    def guarded_get_my_offers(self: Any, *args: Any, **kwargs: Any) -> Any:
        strict = bool(kwargs.get("require_all_endpoints", False))
        if not strict:
            return original_get_my_offers(self, *args, **kwargs)
        token = _STRICT_INVENTORY.set(True)
        try:
            return original_get_my_offers(self, *args, **kwargs)
        finally:
            _STRICT_INVENTORY.reset(token)

    guarded_request._r38_strict_inventory_response_guard = True
    guarded_get_my_offers._r38_strict_inventory_context = True
    client_class._request = guarded_request
    client_class.get_my_offers = guarded_get_my_offers
