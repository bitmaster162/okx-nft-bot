from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


MarketName = Literal["okx", "opensea", "magiceden", "unknown"]
EventType = Literal["sale", "listing", "bid", "transfer", "unknown"]


class RawEvent(BaseModel):
    source: str
    payload: dict[str, Any]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NFTEvent(BaseModel):
    event_id: str
    market: MarketName
    event_type: EventType
    collection: str
    token_id: str
    contract_address: str | None = None
    price: float | None = None
    currency: str | None = None
    quantity: int = 1
    maker: str | None = None
    taker: str | None = None
    tx_hash: str | None = None
    event_time: datetime
    volume_24h: float | None = None
    floor_price: float | None = None
    raw_source: str


class FilterDecision(BaseModel):
    event_id: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)


class DeliveryResult(BaseModel):
    channel: str
    event_id: str
    delivered: bool
    detail: str | None = None
