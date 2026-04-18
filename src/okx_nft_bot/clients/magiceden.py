"""
DEPRECATION WARNING (March 2025):
MagicEden shut down all EVM support (Ethereum, BSC, Polygon) in March 2025.
All EVM API calls to MagicEden are now dead and will return 0 results.

This module is kept for historical reference and potential future use with Solana.
For EVM chains, use alternative marketplaces like OpenSea, Blur, or other Reservoir-based providers.
"""

from __future__ import annotations

from typing import Any, Mapping

from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
from okx_nft_bot.config import Settings


class MagicEdenClient:
    """Magic Eden EVM API client (Reservoir-based).

    DEPRECATED: MagicEden shut down EVM support in March 2025.
    This client is non-functional for EVM chains (Ethereum, BSC, Polygon).
    Solana support may be re-added in the future.
    """

    def __init__(self, settings: Settings, transport: StdlibHttpTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport or StdlibHttpTransport(
            timeout=settings.magiceden_request_timeout,
            max_retries=settings.magiceden_max_retries,
            rate_limit_per_sec=settings.magiceden_rate_limit_per_sec,
        )

    def get_collection_activity(
        self,
        *,
        chain: str,
        collection: str,
        types: list[str] | None = None,
        continuation: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            'collection': collection,
            'limit': limit,
        }
        if continuation:
            params['continuation'] = continuation
        if types:
            # Reservoir uses repeated params: types[]=sale&types[]=transfer
            # We encode them manually in the URL
            base_url, _ = build_url(
                self.settings.magiceden_api_base,
                f'/v3/rtp/{chain}/collections/activity/v6',
                params,
            )
            type_qs = '&'.join(f'types[]={t}' for t in types)
            url = f'{base_url}&{type_qs}'
        else:
            url, _ = build_url(
                self.settings.magiceden_api_base,
                f'/v3/rtp/{chain}/collections/activity/v6',
                params,
            )
        return self._get_url(url)

    def get_collection_listings(
        self,
        *,
        chain: str,
        collection: str,
        continuation: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            'collection': collection,
            'status': 'active',
            'limit': limit,
            'sortBy': 'price',
            'sortDirection': 'asc',
        }
        if continuation:
            params['continuation'] = continuation
        return self._get(f'/v3/rtp/{chain}/orders/asks/v5', params)

    def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        url, _ = build_url(self.settings.magiceden_api_base, path, params)
        return self._get_url(url)

    def _get_url(self, url: str) -> dict[str, Any]:
        headers: dict[str, str] = {'Accept': 'application/json'}
        if self.settings.magiceden_api_key:
            headers['Authorization'] = f'Bearer {self.settings.magiceden_api_key}'
        return self.transport.request_json(method='GET', url=url, headers=headers)
