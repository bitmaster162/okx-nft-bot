from __future__ import annotations

from datetime import datetime, timezone

from okx_nft_bot.models import NFTEvent, RawEvent


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_raw_event(raw_event: RawEvent) -> NFTEvent:
    payload = raw_event.payload
    market = payload.get("market", "unknown")
    event_type = payload.get("event_type", "unknown")
    return NFTEvent(
        event_id=str(payload["event_id"]),
        market=market if market in {"okx", "opensea", "magiceden", "unknown"} else "unknown",
        event_type=event_type if event_type in {"sale", "listing", "bid", "transfer", "unknown"} else "unknown",
        collection=str(payload.get("collection", "unknown")),
        token_id=str(payload.get("token_id", "unknown")),
        contract_address=payload.get("contract_address"),
        price=float(payload["price"]) if payload.get("price") is not None else None,
        currency=payload.get("currency"),
        quantity=int(payload.get("quantity", 1)),
        maker=payload.get("maker"),
        taker=payload.get("taker"),
        tx_hash=payload.get("tx_hash"),
        event_time=_parse_dt(payload.get("event_time")),
        volume_24h=float(payload["volume_24h"]) if payload.get("volume_24h") is not None else None,
        floor_price=float(payload["floor_price"]) if payload.get("floor_price") is not None else None,
        raw_source=raw_event.source,
    )


def normalize_many(raw_events: list[RawEvent]) -> list[NFTEvent]:
    return [normalize_raw_event(raw_event) for raw_event in raw_events]
