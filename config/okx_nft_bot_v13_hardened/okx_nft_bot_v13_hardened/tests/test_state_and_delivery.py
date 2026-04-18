from datetime import datetime, timezone
from pathlib import Path

from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.sqlite import SQLiteStore


def test_state_and_delivery_dedup_roundtrip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.set_state("okx:trades:test", "cursor", "abc123")
    assert store.get_state("okx:trades:test", "cursor") == "abc123"

    event = NFTEvent(
        event_id="evt-1",
        market="okx",
        event_type="sale",
        collection="Test Collection",
        token_id="1",
        event_time=datetime.now(timezone.utc),
        raw_source="test",
    )
    assert store.filter_new_events([event])[0].event_id == "evt-1"
    store.upsert_normalized_events([event])
    assert store.filter_new_events([event]) == []

    assert store.was_notified("telegram", "evt-1") is False
    store.mark_notified("telegram", "evt-1", payload={"x": 1})
    assert store.was_notified("telegram", "evt-1") is True
