from __future__ import annotations

from copy import copy
from functools import wraps
import json
from typing import Any, Mapping

from okx_nft_bot.clients.http import HTTPStatusError, StdlibHttpTransport, build_url


def install_submit_single_attempt_safety(client_class: type[Any]) -> None:
    """Give the exact OKX ``submitOrder`` HTTP effect one transport attempt.

    The legacy request helper retries HTTP 429 responses itself while the shared
    HTTP transport independently retries 429, 5xx, and transport failures. That
    is safe for reads and pre-submit preparation, but not for ``submitOrder``:
    the response can be lost after OKX has already persisted the signed order.

    Install this layer *before* the live-boundary and receipt wrappers so those
    guards remain authoritative around the single marketplace effect.
    """
    original_request = client_class._request
    if getattr(original_request, "_r52_submit_single_attempt_guard", False):
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
        canonical_path = str(path or "").split("?", 1)[0]
        is_submit = (
            str(method or "").upper() == "POST"
            and canonical_path == self._SUBMIT_ORDER_PATH
        )
        if not is_submit:
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

        # Do not mutate the shared transport. Reads and non-submit preparation
        # keep their configured retry policy; only this exact effectful POST is
        # constrained to one production transport attempt.
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

    guarded_request._r52_submit_single_attempt_guard = True
    client_class._request = guarded_request
