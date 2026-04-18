from datetime import datetime, timedelta, timezone
from pathlib import Path

from okx_nft_bot.analytics.cross_market import detect_spreads, rank_collections
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.sqlite import SQLiteStore


def _seed_rows(store: SQLiteStore) -> None:
    now = datetime.now(timezone.utc)
    contract = '0xabc'
    store.upsert_normalized_events([
        NFTEvent(event_id='okx-listing-1', market='okx', event_type='listing', collection='Alpha', token_id='1', contract_address=contract, price=1.0, currency='ETH', event_time=now, volume_24h=120.0, floor_price=1.0, raw_source='x'),
        NFTEvent(event_id='okx-sale-1', market='okx', event_type='sale', collection='Alpha', token_id='2', contract_address=contract, price=1.1, currency='ETH', event_time=now - timedelta(minutes=10), volume_24h=120.0, floor_price=1.0, raw_source='x'),
        NFTEvent(event_id='os-listing-1', market='opensea', event_type='listing', collection='Alpha', token_id='1', contract_address=contract, price=1.3, currency='ETH', event_time=now - timedelta(minutes=5), volume_24h=80.0, floor_price=1.25, raw_source='y'),
        NFTEvent(event_id='os-sale-1', market='opensea', event_type='sale', collection='Alpha', token_id='3', contract_address=contract, price=1.28, currency='ETH', event_time=now - timedelta(minutes=20), volume_24h=80.0, floor_price=1.25, raw_source='y'),
    ])


def test_detect_spreads_finds_cross_market_gap(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / 'db.sqlite3')
    _seed_rows(store)
    rows = store.fetch_analysis_events(limit=100)
    spreads = detect_spreads(rows, min_spread_pct=5.0, top_n=10)
    assert spreads
    top = spreads[0]
    assert top.buy_market == 'okx'
    assert top.sell_market == 'opensea'
    assert top.spread_pct >= 20.0


def test_rank_collections_scores_cross_market_activity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / 'db.sqlite3')
    _seed_rows(store)
    rows = store.fetch_analysis_events(limit=100)
    rankings = rank_collections(rows, min_spread_pct=5.0, top_n=10)
    assert rankings
    top = rankings[0]
    assert top.collection_name == 'Alpha'
    assert top.market_count == 2
    assert top.best_spread_pct >= 20.0
    assert top.score > 0
