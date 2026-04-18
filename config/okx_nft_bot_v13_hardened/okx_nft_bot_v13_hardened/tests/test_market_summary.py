from datetime import datetime, timezone
from pathlib import Path

from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.sqlite import SQLiteStore


def test_market_summary_groups_by_market_and_collection(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / 'db.sqlite3')
    now = datetime.now(timezone.utc)
    store.upsert_normalized_events([
        NFTEvent(event_id='1', market='okx', event_type='sale', collection='Alpha', token_id='1', event_time=now, raw_source='x', price=1.0),
        NFTEvent(event_id='2', market='okx', event_type='sale', collection='Alpha', token_id='2', event_time=now, raw_source='x', price=2.0),
        NFTEvent(event_id='3', market='opensea', event_type='listing', collection='Alpha', token_id='3', event_time=now, raw_source='y', price=3.0),
    ])
    rows = store.fetch_market_summary()
    assert any(row['market'] == 'okx' and row['event_count'] == 2 for row in rows)
    assert any(row['market'] == 'opensea' and row['event_count'] == 1 for row in rows)
