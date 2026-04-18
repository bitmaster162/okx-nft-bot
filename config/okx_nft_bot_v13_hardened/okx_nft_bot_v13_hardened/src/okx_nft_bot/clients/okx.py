from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
from okx_nft_bot.config import Settings


class OKXMarketplaceClient:
    def __init__(self, settings: Settings, transport: StdlibHttpTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport or StdlibHttpTransport(
            timeout=settings.okx_request_timeout,
            max_retries=settings.okx_max_retries,
            rate_limit_per_sec=settings.okx_rate_limit_per_sec,
        )

    def get_collection_trades(
        self,
        *,
        chain: str,
        collection_address: str,
        platform: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/markets/trades",
            {
                "chain": chain,
                "collectionAddress": collection_address,
                "platform": platform,
                "limit": limit,
                "cursor": cursor,
                "startTime": start_time,
                "endTime": end_time,
            },
        )

    def get_collection_list(
        self,
        *,
        chain: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/collection/list",
            {
                "chain": chain,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_listings(
        self,
        *,
        chain: str,
        collection_address: str | None = None,
        token_id: str | None = None,
        platform: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/markets/listings",
            {
                "chain": chain,
                "collectionAddress": collection_address,
                "tokenId": token_id,
                "platform": platform,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_collection_details(self, *, slug: str) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/collection/detail",
            {"slug": slug},
        )

    def get_nft_details(self, *, chain: str, contract_address: str, token_id: str) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/asset/detail",
            {
                "chain": chain,
                "contractAddress": contract_address,
                "tokenId": token_id,
            },
        )

    def get_nft_list(
        self,
        *,
        chain: str,
        contract_address: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/asset/list",
            {
                "chain": chain,
                "contractAddress": contract_address,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_offers(
        self,
        *,
        chain: str,
        collection_address: str | None = None,
        token_id: str | None = None,
        maker: str | None = None,
        status: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/markets/offers",
            {
                "chain": chain,
                "collectionAddress": collection_address,
                "tokenId": token_id,
                "maker": maker,
                "status": status,
                "sort": sort,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def get_collection_offers(
        self,
        *,
        chain: str,
        collection_address: str | None = None,
        maker: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/api/v5/mktplace/nft/markets/collection-offers",
            {
                "chain": chain,
                "collectionAddress": collection_address,
                "maker": maker,
                "status": status,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.settings.okx_api_key or not self.settings.okx_api_secret or not self.settings.okx_api_passphrase:
            raise RuntimeError("Missing OKX API credentials in environment")
        url, request_path = build_url(self.settings.okx_api_base, path, params)
        body = ""
        headers = self._build_headers(method="GET", request_path=request_path, body=body)
        payload = self.transport.request_json(method="GET", url=url, headers=headers, body=body)
        return payload

    def _build_headers(self, *, method: str, request_path: str, body: str) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        signature = sign_okx_request(
            secret_key=self.settings.okx_api_secret or "",
            timestamp=timestamp,
            method=method,
            request_path=request_path,
            body=body,
        )
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "OK-ACCESS-KEY": self.settings.okx_api_key or "",
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.okx_api_passphrase or "",
        }


def sign_okx_request(*, secret_key: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
