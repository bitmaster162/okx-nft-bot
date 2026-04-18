from datetime import datetime, timezone

from okx_nft_bot.models import RawEvent
from okx_nft_bot.pipeline.normalize import normalize_raw_event


def test_normalize_raw_event_maps_expected_fields() -> None:
    raw = RawEvent(
        source="test",
        fetched_at=datetime.now(timezone.utc),
        payload={
            "event_id": "evt-1",
            "market": "okx",
            "event_type": "sale",
            "collection": "Cool Collection",
            "token_id": "777",
            "price": 1.23,
            "currency": "ETH",
            "event_time": "2026-03-07T10:00:00+00:00",
            "volume_24h": 100.0,
            "floor_price": 1.0,
        },
    )

    event = normalize_raw_event(raw)

    assert event.event_id == "evt-1"
    assert event.market == "okx"
    assert event.event_type == "sale"
    assert event.collection == "Cool Collection"
    assert event.token_id == "777"
    assert event.price == 1.23
    assert event.currency == "ETH"
    assert event.volume_24h == 100.0
    assert event.floor_price == 1.0
