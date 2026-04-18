from __future__ import annotations

import logging
from typing import Any

from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.normalizers.offers import NormalizedOffer, normalize_offer

logger = logging.getLogger(__name__)


class OKXOffersProvider:
    """Read-only provider that fetches NFT offers from OKX marketplace.

    Can be scoped to specific maker addresses (e.g. parasite wallets) or
    a specific collection. No write/submit paths exist in this class.
    """

    def __init__(self, client: OKXMarketplaceClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_offers(
        self,
        *,
        chain: str | None = None,
        collection_address: str | None = None,
        maker: str | None = None,
        status: str = "active",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one page of offers. Returns {'offers': [...], 'next_cursor': str|None}."""
        resolved_chain = chain or self.settings.okx_chain
        resolved_limit = limit or self.settings.okx_page_limit
        resolved_collection = collection_address or self.settings.okx_collection_address

        try:
            payload = self.client.get_offers(
                chain=resolved_chain,
                collection_address=resolved_collection,
                maker=maker,
                status=status,
                limit=resolved_limit,
                cursor=cursor,
            )
        except Exception:
            logger.exception("OKX get_offers failed (chain=%s, collection=%s, maker=%s)", resolved_chain, resolved_collection, maker)
            return {"offers": [], "next_cursor": None}

        raw_items = _extract_records(payload)
        next_cursor = _extract_cursor(payload)
        offers: list[NormalizedOffer] = []
        for raw in raw_items:
            try:
                offers.append(normalize_offer(raw, market="okx", chain=resolved_chain))
            except Exception:
                logger.exception("Failed to normalize OKX offer: %r", raw)
        return {"offers": offers, "next_cursor": next_cursor}

    def fetch_all_pages(
        self,
        *,
        chain: str | None = None,
        collection_address: str | None = None,
        maker: str | None = None,
        status: str = "active",
        max_pages: int = 5,
    ) -> list[NormalizedOffer]:
        """Fetch up to max_pages of offers, following cursor pagination."""
        all_offers: list[NormalizedOffer] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = self.fetch_offers(
                chain=chain,
                collection_address=collection_address,
                maker=maker,
                status=status,
                cursor=cursor,
            )
            all_offers.extend(page["offers"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        return all_offers


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        inner = data.get("data") or data.get("orders") or data.get("records")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        return [data] if data else []
    return []


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        cursor = data.get("cursor") or data.get("nextCursor")
        return None if cursor in {None, "", "0"} else str(cursor)
    return None
