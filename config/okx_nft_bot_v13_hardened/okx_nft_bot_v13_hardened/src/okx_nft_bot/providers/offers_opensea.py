from __future__ import annotations

import logging
from typing import Any

from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import Settings
from okx_nft_bot.normalizers.offers import NormalizedOffer, normalize_offer

logger = logging.getLogger(__name__)


class OpenSeaOffersProvider:
    """Read-only provider that fetches collection offers from OpenSea v2 API.

    No write/submit paths exist in this class.
    """

    def __init__(self, client: OpenSeaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_offers(
        self,
        *,
        slug: str | None = None,
        chain: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch one page of offers. Returns {'offers': [...], 'next_cursor': str|None}."""
        resolved_slug = slug or self.settings.opensea_collection_slug
        resolved_chain = chain or self.settings.opensea_chain
        resolved_limit = limit or self.settings.opensea_page_limit

        if not resolved_slug:
            logger.warning("OpenSeaOffersProvider: no slug configured, skipping")
            return {"offers": [], "next_cursor": None}

        try:
            payload = self.client.get_offers(slug=resolved_slug, cursor=cursor, limit=resolved_limit)
        except Exception:
            logger.exception("OpenSea get_offers failed (slug=%s)", resolved_slug)
            return {"offers": [], "next_cursor": None}

        raw_items = _extract_records(payload)
        next_cursor = _extract_cursor(payload)
        offers: list[NormalizedOffer] = []
        for raw in raw_items:
            try:
                offers.append(normalize_offer(raw, market="opensea", chain=resolved_chain))
            except Exception:
                logger.exception("Failed to normalize OpenSea offer: %r", raw)
        return {"offers": offers, "next_cursor": next_cursor}

    def fetch_all_pages(
        self,
        *,
        slug: str | None = None,
        chain: str | None = None,
        max_pages: int = 5,
    ) -> list[NormalizedOffer]:
        """Fetch up to max_pages of offers, following cursor pagination."""
        all_offers: list[NormalizedOffer] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = self.fetch_offers(slug=slug, chain=chain, cursor=cursor)
            all_offers.extend(page["offers"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        return all_offers


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # OpenSea v2: {"orders": [...], "next": "cursor"}
    orders = payload.get("orders") or payload.get("data") or []
    if isinstance(orders, list):
        return [item for item in orders if isinstance(item, dict)]
    return []


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    cursor = payload.get("next")
    return None if not cursor else str(cursor)
