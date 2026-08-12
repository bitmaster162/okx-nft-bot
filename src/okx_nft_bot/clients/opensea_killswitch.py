from __future__ import annotations

from copy import copy
import json
import os
import re
from typing import Any, Mapping

from okx_nft_bot.clients.http import HTTPStatusError, StdlibHttpTransport, build_url
from okx_nft_bot.clients.opensea import SEAPORT_ADDRESS_ETH
from okx_nft_bot.config import Settings


_ORDER_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})


def _opensea_chain(chain: str) -> str:
    resolved = str(chain).strip().lower()
    if resolved in {"eth", "ethereum", "1"}:
        return "ethereum"
    raise ValueError(f"OpenSea kill-switch cancellation only supports Ethereum; got {chain!r}")


def _require_order_hash(order_hash: str) -> str:
    resolved = str(order_hash).strip()
    if not _ORDER_HASH_RE.fullmatch(resolved):
        raise ValueError("OpenSea order hash must be a 32-byte 0x-prefixed hex value")
    return resolved.lower()


def _order_is_cancelled(payload: Mapping[str, Any]) -> bool:
    """Accept only explicit cancellation state from the exact-order readback."""
    candidates: list[Mapping[str, Any]] = [payload]
    for key in ("order", "offer", "listing"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)

    for candidate in candidates:
        for key in ("canceled", "cancelled"):
            if candidate.get(key) is True:
                return True
        for key in ("status", "order_status"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip().lower() in _CANCELLED_STATUSES:
                return True
    return False


class OpenSeaKillSwitchClient:
    """Off-chain SignedZone cancellation for already-tracked OpenSea orders.

    This adapter deliberately does not mint/exchange auth tokens and never signs
    wallet messages or transactions. A pre-provisioned short-lived wallet JWT is
    read from ``OPENSEA_WALLET_JWT`` at construction time. Cancellation remains
    fail-closed unless the exact order readback explicitly reports cancellation.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        transport: Any | None = None,
        wallet_jwt: str | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or StdlibHttpTransport(
            timeout=settings.opensea_request_timeout,
            max_retries=settings.opensea_max_retries,
            rate_limit_per_sec=settings.opensea_rate_limit_per_sec,
        )
        self.wallet_jwt = (wallet_jwt or os.getenv("OPENSEA_WALLET_JWT") or "").strip() or None

    def _headers(self) -> dict[str, str]:
        api_key = (self.settings.opensea_api_key or "").strip()
        if not api_key:
            raise RuntimeError("OPENSEA_API_KEY not configured for kill-switch cancellation")
        if not self.wallet_jwt:
            raise RuntimeError("OPENSEA_WALLET_JWT not configured for kill-switch cancellation")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-KEY": api_key,
            "Authorization": f"Bearer {self.wallet_jwt}",
        }

    def _request_cancel_once(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
    ) -> None:
        """Cross the off-chain cancel boundary at most once in production."""
        if not isinstance(self.transport, StdlibHttpTransport):
            # Injected transports retain their own contract. This adapter calls
            # them exactly once; production retry policy is handled below.
            self.transport.request_json(
                method="POST",
                url=url,
                headers=headers,
                body="{}",
            )
            return

        isolated = copy(self.transport)
        isolated.max_retries = 1
        isolated._rate_limiter.wait()
        try:
            response = isolated._session.request(
                method="POST",
                url=url,
                headers=dict(headers),
                data=b"{}",
                timeout=isolated.timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"OpenSea cancellation transport failed: {exc}") from exc
        if not response.ok:
            raise HTTPStatusError(
                status=response.status_code,
                body=response.text,
                headers=dict(response.headers),
            )

    def cancel_offer(self, order_hash: str, *, chain: str = "eth") -> bool:
        opensea_chain = _opensea_chain(chain)
        resolved_hash = _require_order_hash(order_hash)
        headers = self._headers()
        base = self.settings.opensea_api_base
        path = (
            f"/api/v2/orders/chain/{opensea_chain}/protocol/"
            f"{SEAPORT_ADDRESS_ETH}/{resolved_hash}"
        )
        cancel_url, _ = build_url(base, f"{path}/cancel")
        order_url, _ = build_url(base, path)

        # The SignedZone cancel endpoint is effectful. Never blind-retry it.
        self._request_cancel_once(url=cancel_url, headers=headers)

        # HTTP 200 is not sufficient proof. Re-read this exact protocol/order
        # tuple and accept success only on an explicit cancellation state.
        readback = self.transport.request_json(
            method="GET",
            url=order_url,
            headers={
                "Accept": "application/json",
                "X-API-KEY": headers["X-API-KEY"],
            },
            body="",
        )
        if not isinstance(readback, Mapping) or not _order_is_cancelled(readback):
            detail = json.dumps(readback, sort_keys=True, default=str)[:300]
            raise RuntimeError(
                "OpenSea cancellation post-condition not confirmed by exact-order readback: "
                f"{detail}"
            )
        return True
