from __future__ import annotations

from copy import copy
from functools import wraps
import json
import logging
from typing import Any, Mapping

from okx_nft_bot.clients.http import HTTPStatusError, StdlibHttpTransport, build_url


log = logging.getLogger("counterbid.cancel_effect_safety")

_CANCEL_PATH = "/api/v5/mktplace/nft/markets/cancel-listing"
_AMBIGUOUS_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_ACK_KEYS = ("success", "cancelled", "result")


def _http_status_from_exception(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPStatusError):
            return int(current.status)
        current = current.__cause__ or current.__context__
    return None


def _ambiguous_cancel_failure(exc: BaseException) -> bool:
    from okx_nft_bot.counterbid.okx_api import OKXNetworkError, OKXRateLimitError

    status = _http_status_from_exception(exc)
    if status is not None:
        return status in _AMBIGUOUS_HTTP_STATUSES or status >= 500
    return isinstance(exc, (OKXNetworkError, OKXRateLimitError)) or status is None


def _ack_value(response: Mapping[str, Any]) -> Any:
    for key in _ACK_KEYS:
        if key in response:
            return response[key]

    data = response.get("data")
    if isinstance(data, Mapping):
        for key in _ACK_KEYS:
            if key in data:
                return data[key]
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, Mapping):
                continue
            for key in _ACK_KEYS:
                if key in item:
                    return item[key]
    return None


def _confirmed_cancel_ack(response: Mapping[str, Any]) -> bool:
    """Return True only for a code=0 receipt with explicit affirmative cancellation acknowledgement."""
    if str(response.get("code", "")) != "0":
        return False

    success = _ack_value(response)
    if success is None:
        return False
    if isinstance(success, bool):
        return success
    return str(success).lower() not in {"0", "false", "failed"}


def _strict_active_readback(self: Any, *, offer_id: str, chain: str) -> bool | None:
    """Return True when the exact offer is still active; None when unresolved.

    Absence is deliberately not promoted to cancellation success because the
    current inventory API is page-limited. The readback is still useful to prove
    that an ambiguous cancel did *not* remove the exact order. Either way the
    caller retains local exposure and never crosses a second effect boundary.
    """
    try:
        records = self.get_my_offers(
            chain=chain,
            require_all_endpoints=True,
        )
    except Exception as exc:
        log.warning(
            "cancel_offer %s: strict reconciliation failed after ambiguous API cancel: %s",
            offer_id[:14],
            exc,
        )
        return None

    expected = str(offer_id).strip()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        candidate = str(
            record.get("orderId")
            or record.get("offerId")
            or record.get("orderHash")
            or record.get("id")
            or ""
        ).strip()
        if candidate == expected:
            log.warning(
                "cancel_offer %s: strict reconciliation still sees exact active order; retaining exposure",
                offer_id[:14],
            )
            return True

    log.warning(
        "cancel_offer %s: strict reconciliation did not find exact order, but absence is not authoritative; retaining exposure",
        offer_id[:14],
    )
    return None


def install_cancel_effect_safety(client_class: type[Any]) -> None:
    """Make OKX cancellation single-attempt and fail closed on ambiguous receipts.

    ``cancel-listing`` is effectful. The legacy ``_request`` helper has an outer
    429 loop and the shared production transport independently retries 429, 5xx,
    and transport failures. Worse, ``cancel_offer`` collapsed every API exception
    to ``False`` and could immediately send an on-chain Seaport cancellation.

    R57 constrains the exact HTTP cancellation boundary to one production
    transport attempt. Ambiguous failures get read-only strict inventory
    reconciliation and retain exposure; they never trigger the on-chain fallback.
    Deterministic API rejections retain the existing Seaport fallback behavior.
    """
    current_request = client_class._request
    current_cancel = client_class.cancel_offer
    request_installed = bool(getattr(current_request, "_r57_cancel_single_attempt_guard", False))
    cancel_installed = bool(getattr(current_cancel, "_r57_cancel_ambiguity_guard", False))
    if request_installed and cancel_installed:
        return
    if request_installed or cancel_installed:
        raise RuntimeError("partial R57 OKX cancel effect safety installation detected")

    original_request = current_request
    original_cancel = current_cancel

    @wraps(original_request)
    def guarded_request(
        self: Any,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        canonical_path = str(path or "").split("?", 1)[0]
        is_cancel = str(method or "").upper() == "POST" and canonical_path == _CANCEL_PATH
        if not is_cancel:
            return original_request(
                self,
                method=method,
                path=path,
                params=params,
                payload=payload,
            )

        client = self._market_client()
        body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        url, request_path = build_url(self.settings.okx_api_base, path, params)
        headers = client._build_headers(
            method="POST",
            request_path=request_path,
            body=body,
        )

        transport = client.transport
        if isinstance(transport, StdlibHttpTransport):
            transport = copy(transport)
            transport.max_retries = 1

        try:
            return transport.request_json(
                method="POST",
                url=url,
                headers=headers,
                body=body,
            )
        except HTTPStatusError as exc:
            from okx_nft_bot.counterbid.okx_api import (
                OKXAuthError,
                OKXRateLimitError,
                OKXSubmitError,
            )

            if exc.status == 429:
                raise OKXRateLimitError(str(exc)) from exc
            if exc.status in {401, 403}:
                raise OKXAuthError(str(exc)) from exc
            raise OKXSubmitError(str(exc)) from exc
        except RuntimeError as exc:
            from okx_nft_bot.counterbid.okx_api import OKXNetworkError

            raise OKXNetworkError(str(exc)) from exc

    @wraps(original_cancel)
    def guarded_cancel_offer(
        self: Any,
        offer_id: str,
        chain: str = "bsc",
        order_params: dict | None = None,
    ) -> bool:
        if not self.settings.buyer_wallet_address:
            return original_cancel(
                self,
                offer_id,
                chain=chain,
                order_params=order_params,
            )

        chain_name = {
            "eth": "eth",
            "bsc": "bsc",
            "polygon": "polygon",
        }.get(str(chain).lower(), str(chain).lower())
        payload = {
            "chain": chain_name,
            "walletAddress": self.settings.buyer_wallet_address,
            "orderIds": [str(offer_id)],
        }

        try:
            response = self._request(
                method="POST",
                path=_CANCEL_PATH,
                payload=payload,
            )
        except Exception as exc:
            if _ambiguous_cancel_failure(exc):
                _strict_active_readback(self, offer_id=str(offer_id), chain=chain_name)
                log.warning(
                    "cancel_offer %s: API cancellation receipt is ambiguous; on-chain fallback suppressed",
                    str(offer_id)[:14],
                )
                return False

            log.info(
                "cancel_offer %s: deterministic API rejection; on-chain fallback eligible",
                str(offer_id)[:14],
            )
            if order_params:
                return self._cancel_onchain_seaport(order_params, chain_name)
            return False

        if not isinstance(response, Mapping):
            _strict_active_readback(self, offer_id=str(offer_id), chain=chain_name)
            log.warning(
                "cancel_offer %s: malformed API cancellation receipt; on-chain fallback suppressed",
                str(offer_id)[:14],
            )
            return False

        resp_code = str(response.get("code", "0"))
        resp_msg = str(response.get("msg", "") or response.get("message", ""))
        deterministic_reject = "no longer" in resp_msg.lower() or resp_code != "0"
        if deterministic_reject:
            if order_params:
                return self._cancel_onchain_seaport(order_params, chain_name)
            return False

        if not _confirmed_cancel_ack(response):
            _strict_active_readback(self, offer_id=str(offer_id), chain=chain_name)
            log.warning(
                "cancel_offer %s: cancellation not explicitly acknowledged; on-chain fallback suppressed",
                str(offer_id)[:14],
            )
            return False

        log.info("cancel_offer %s: SUCCESS via single-attempt API cancel", str(offer_id)[:14])
        return True

    guarded_request._r57_cancel_single_attempt_guard = True
    guarded_cancel_offer._r57_cancel_ambiguity_guard = True
    client_class._request = guarded_request
    client_class.cancel_offer = guarded_cancel_offer
