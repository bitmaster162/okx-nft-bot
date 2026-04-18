from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.models import RawEvent
from okx_nft_bot.providers.base import Provider


class OKXMarketplaceTradesProvider(Provider):
    def __init__(self, client: OKXMarketplaceClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        page = self.fetch_page(cursor=None)
        return page["events"]

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        if not self.settings.okx_collection_address:
            raise RuntimeError("OKX_COLLECTION_ADDRESS is required for live provider")

        trades_payload = self.client.get_collection_trades(
            chain=self.settings.okx_chain,
            collection_address=self.settings.okx_collection_address,
            platform=self.settings.okx_platform,
            limit=self.settings.okx_page_limit,
            cursor=cursor,
        )
        trades = list(_extract_records(trades_payload))
        next_cursor = _extract_cursor(trades_payload)

        collection_details: dict[str, Any] | None = None
        if self.settings.okx_collection_slug:
            details_payload = self.client.get_collection_details(slug=self.settings.okx_collection_slug)
            detail_records = list(_extract_records(details_payload))
            collection_details = detail_records[0] if detail_records else None

        events: list[RawEvent] = []
        for trade in trades:
            enriched_asset: dict[str, Any] | None = None
            if self.settings.okx_enable_details:
                token_id = trade.get("tokenId")
                if token_id is not None:
                    try:
                        detail_payload = self.client.get_nft_details(
                            chain=self.settings.okx_chain,
                            contract_address=self.settings.okx_collection_address,
                            token_id=str(token_id),
                        )
                        detail_records = list(_extract_records(detail_payload))
                        enriched_asset = detail_records[0] if detail_records else None
                    except Exception:
                        enriched_asset = None

            payload = {
                "market": "okx",
                "event_type": "sale",
                "event_id": _build_trade_event_id(trade),
                "collection": _collection_name_from_enrichment(collection_details, trade),
                "token_id": str(trade.get("tokenId", "unknown")),
                "contract_address": trade.get("collectionAddress"),
                "price": _to_float(trade.get("price")),
                "currency": trade.get("currencyAddress"),
                "quantity": int(trade.get("amount", 1) or 1),
                "maker": trade.get("from"),
                "taker": trade.get("to"),
                "tx_hash": trade.get("txHash"),
                "event_time": _timestamp_to_iso(trade.get("timestamp")),
                "floor_price": _to_float(collection_details.get("floorPrice") if collection_details else None),
                "volume_24h": _to_float(collection_details.get("volume24h") if collection_details else None),
                "trade": trade,
                "collection_details": collection_details,
                "asset_details": enriched_asset,
                "next_cursor": next_cursor,
            }
            events.append(RawEvent(source="okx_marketplace_trades", payload=payload))
        return {"events": events, "next_cursor": next_cursor}


class OKXMarketplaceListingsProvider(Provider):
    def __init__(self, client: OKXMarketplaceClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        page = self.fetch_page(cursor=None)
        return page["events"]

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        payload = self.client.get_listings(
            chain=self.settings.okx_chain,
            collection_address=self.settings.okx_collection_address,
            platform=self.settings.okx_platform,
            limit=self.settings.okx_page_limit,
            cursor=cursor,
        )
        items = list(_extract_records(payload))
        next_cursor = _extract_cursor(payload)
        events = [
            RawEvent(
                source="okx_marketplace_listings",
                payload={
                    "event_id": _build_listing_event_id(item),
                    "market": "okx",
                    "event_type": "listing",
                    "collection": self.settings.okx_collection_slug or self.settings.okx_collection_address or "unknown",
                    "token_id": str(item.get("tokenId", "unknown")),
                    "contract_address": item.get("collectionAddress"),
                    "price": _to_float(item.get("price")),
                    "currency": item.get("currencyAddress"),
                    "quantity": int(item.get("amount", 1) or 1),
                    "maker": item.get("maker"),
                    "tx_hash": item.get("orderHash"),
                    "event_time": _timestamp_to_iso(item.get("listingTime") or item.get("createTime")),
                    "listing": item,
                    "next_cursor": next_cursor,
                },
            )
            for item in items
        ]
        return {"events": events, "next_cursor": next_cursor}


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        return [data]
    return []


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        cursor = data.get("cursor") or data.get("nextCursor")
        return None if cursor in {None, "", "0"} else str(cursor)
    return None


def _timestamp_to_iso(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    ts = int(value)
    if ts > 10_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_trade_event_id(trade: dict[str, Any]) -> str:
    return f"sale:{trade.get('txHash','nohash')}:{trade.get('tokenId','unknown')}:{trade.get('timestamp','0')}"


def _build_listing_event_id(item: dict[str, Any]) -> str:
    return f"listing:{item.get('orderHash','nohash')}:{item.get('tokenId','unknown')}:{item.get('listingTime') or item.get('createTime') or '0'}"


def _collection_name_from_enrichment(collection_details: dict[str, Any] | None, trade: dict[str, Any]) -> str:
    if collection_details and collection_details.get("name"):
        return str(collection_details["name"])
    return str(trade.get("collectionAddress", "unknown"))
