from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class NormalizedOffer(BaseModel):
    market: str
    collection_slug_or_address: str
    chain: str
    token_id: str | None = None  # None for collection-wide offers
    offer_id: str
    maker: str | None = None
    price: float | None = None
    currency: str | None = None
    quantity: int = 1
    status: str = "active"
    created_at: datetime | None = None
    expires_at: datetime | None = None
    raw_payload_hash: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_type: str  # "collection_offer" or "token_offer"
    source_reliability: str  # "high", "medium", "low"


def normalize_offer(raw: dict[str, Any], *, market: str, chain: str) -> NormalizedOffer:
    if market == "okx":
        return _normalize_okx(raw, chain=chain)
    if market == "opensea":
        return _normalize_opensea(raw, chain=chain)
    raise ValueError(f"Unsupported market for offer normalization: {market!r}")


def _has_token_identifier(value: Any) -> bool:
    """Return True for an explicit token identifier, including numeric/string zero.

    Token #0 is a specific NFT item. Only explicit absence (None/empty string)
    denotes no token identifier at this normalization boundary.
    """
    return value is not None and value != ""


def _normalize_okx(raw: dict[str, Any], *, chain: str) -> NormalizedOffer:
    token_id = raw.get("tokenId")
    has_token_id = _has_token_identifier(token_id)
    collection = str(raw.get("collectionAddress") or "unknown").lower()
    offer_id = str(
        raw.get("orderId") or raw.get("orderHash") or f"okx:{collection}:{token_id}:{raw.get('createTime','0')}"
    )
    price_raw = raw.get("price")
    price = _to_float(price_raw)
    source_type = "token_offer" if has_token_id else "collection_offer"
    return NormalizedOffer(
        market="okx",
        collection_slug_or_address=collection,
        chain=chain,
        token_id=str(token_id) if has_token_id else None,
        offer_id=offer_id,
        maker=raw.get("maker"),
        price=price,
        currency=raw.get("currencyAddress"),
        quantity=int(raw.get("amount", 1) or 1),
        status=str(raw.get("status") or "active"),
        created_at=_ts_to_dt(raw.get("createTime")),
        expires_at=_ts_to_dt(raw.get("expirationTime")),
        raw_payload_hash=_hash_payload(raw),
        source_type=source_type,
        source_reliability="high",
    )


def _opensea_offer_id(raw: dict[str, Any]) -> str:
    order_hash = raw.get("order_hash") or raw.get("orderHash")
    if order_hash:
        return str(order_hash)
    serialized = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    return f"opensea:sha256:{digest}"


def _normalize_opensea(raw: dict[str, Any], *, chain: str) -> NormalizedOffer:
    # OpenSea v2 offer shape: order_hash, maker (dict or str), current_price, protocol_data, etc.
    order_hash = _opensea_offer_id(raw)
    protocol_data = raw.get("protocol_data") or {}
    parameters = protocol_data.get("parameters") or {}
    consideration = parameters.get("consideration") or []
    token_id: str | None = None
    collection_addr = "unknown"
    specific_token_seen = False
    for item in consideration:
        try:
            item_type = int(item.get("itemType", 0))
        except (TypeError, ValueError):
            item_type = 0
        if item_type in (2, 3):  # ERC721 or ERC1155 — specific token
            specific_token_seen = True
            identifier = item.get("identifierOrCriteria")
            if not _has_token_identifier(identifier):
                identifier = item.get("identifier")
            if _has_token_identifier(identifier):
                token_id = str(identifier)
            collection_addr = str(item.get("token") or "").lower()
            break
        if item_type in (4, 5):  # criteria-based = collection offer
            collection_addr = str(item.get("token") or "").lower()
    # Fallback collection identification
    if collection_addr == "unknown":
        collection_addr = str(raw.get("collection") or raw.get("slug") or "unknown")
    maker_raw = raw.get("maker")
    if isinstance(maker_raw, dict):
        maker = maker_raw.get("address")
    else:
        maker = maker_raw
    price_raw = raw.get("current_price") or raw.get("price")
    price = _to_float(price_raw)
    # OpenSea prices are in wei for ETH — convert to ETH
    if price is not None and price > 1e15:
        price = price / 1e18
    expiration = raw.get("expiration_time") or raw.get("expiry")
    created = raw.get("listing_time") or raw.get("created_date")
    source_type = "token_offer" if specific_token_seen else "collection_offer"
    return NormalizedOffer(
        market="opensea",
        collection_slug_or_address=collection_addr,
        chain=chain,
        token_id=token_id,
        offer_id=order_hash,
        maker=maker,
        price=price,
        currency="ETH",
        quantity=1,
        status="active",
        created_at=_flexible_dt(created),
        expires_at=_flexible_dt(expiration),
        raw_payload_hash=_hash_payload(raw),
        source_type=source_type,
        source_reliability="high",
    )


def _hash_payload(raw: dict[str, Any]) -> str:
    serialized = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts_to_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _flexible_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _ts_to_dt(value)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return _ts_to_dt(int(text))
    except (TypeError, ValueError):
        return None
