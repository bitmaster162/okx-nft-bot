from __future__ import annotations

from pathlib import Path

import okx_nft_bot.pipeline.live_cycle as live_cycle
from okx_nft_bot.config import Settings, load_settings
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.pipeline.live_cycle import Monitor
from okx_nft_bot.storage.sqlite import SQLiteStore


class FakeOKXClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.trade_calls: list[str | None] = []

    def get_collection_trades(
        self,
        *,
        chain: str,
        collection_address: str,
        platform: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        self.trade_calls.append(cursor)
        return self.pages[len(self.trade_calls) - 1]


class FakeOpenSeaClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.event_calls: list[str | None] = []
        self.stats_calls = 0
        self.collection_calls = 0

    def get_collection_events(
        self,
        *,
        slug: str,
        event_type: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        self.event_calls.append(cursor)
        return self.pages[len(self.event_calls) - 1]

    def get_collection_stats(self, *, slug: str) -> dict[str, object]:
        self.stats_calls += 1
        return {'floor_price': 1.1, 'total': {'one_day': 12.3}}

    def get_collection(self, *, slug: str) -> dict[str, object]:
        self.collection_calls += 1
        return {'collection': {'name': 'Pudgy Penguins'}}


def _okx_trade_page(page_no: int, *, next_cursor: str | None) -> dict[str, object]:
    return {
        'data': {
            'data': [
                {
                    'txHash': f'0xtx{page_no}',
                    'tokenId': str(page_no),
                    'timestamp': 1_710_000_000 + page_no,
                    'collectionAddress': '0xabc',
                    'price': '1.5',
                    'currencyAddress': 'ETH',
                    'amount': '1',
                    'from': '0xseller',
                    'to': '0xbuyer',
                }
            ],
            'cursor': next_cursor,
        }
    }


def _opensea_sale_page(page_no: int, *, next_cursor: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        'asset_events': [
            {
                'event_id': f'ev{page_no}',
                'event_timestamp': f'2026-03-0{page_no}T00:00:00Z',
                'sale_price': '1.5',
                'payment': {'symbol': 'ETH'},
                'seller': {'address': '0xseller'},
                'buyer': {'address': '0xbuyer'},
                'transaction': f'0xtx{page_no}',
                'nft': {'identifier': str(page_no), 'contract': '0xabc'},
            }
        ],
    }
    if next_cursor is not None:
        payload['next'] = next_cursor
    return payload


def test_load_settings_reads_opensea_max_pages_per_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('PROFILES_DIR', str(tmp_path / 'profiles'))
    monkeypatch.setenv('OKX_MAX_PAGES_PER_RUN', '9')
    monkeypatch.setenv('OPENSEA_MAX_PAGES_PER_RUN', '2')

    settings = load_settings()

    assert settings.okx_max_pages_per_run == 9
    assert settings.opensea_max_pages_per_run == 2


def test_load_settings_opensea_default_is_independent_from_okx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('PROFILES_DIR', str(tmp_path / 'profiles'))
    monkeypatch.setenv('OKX_MAX_PAGES_PER_RUN', '9')
    monkeypatch.delenv('OPENSEA_MAX_PAGES_PER_RUN', raising=False)

    settings = load_settings()

    assert settings.okx_max_pages_per_run == 9
    assert settings.opensea_max_pages_per_run == 5


def test_monitor_uses_okx_max_pages_per_run_via_provider(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        app_env='test',
        db_path=tmp_path / 'db.sqlite3',
        active_market='okx',
        okx_collection_address='0xabc',
        okx_max_pages_per_run=2,
        opensea_max_pages_per_run=2,
        rules_path=tmp_path / 'rule_packs.json',
    )
    store = SQLiteStore(settings.db_path)
    monitor = Monitor(settings=settings, store=store, notifier=NullNotifier())
    fake_client = FakeOKXClient(
        pages=[
            _okx_trade_page(1, next_cursor='cursor-2'),
            _okx_trade_page(2, next_cursor='cursor-3'),
            _okx_trade_page(3, next_cursor='cursor-4'),
        ]
    )
    monkeypatch.setattr(live_cycle, 'OKXMarketplaceClient', lambda settings: fake_client)

    result = monitor.run_live_cycle(source_mode='trades')

    assert result.pages_fetched == 2
    assert fake_client.trade_calls == [None, 'cursor-2']
    assert result.end_cursor == 'cursor-3'
    assert len(result.new_events) == 2
    assert store.count_events() == 2


def test_monitor_uses_opensea_max_pages_per_run_via_provider(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        app_env='test',
        db_path=tmp_path / 'db.sqlite3',
        active_market='opensea',
        opensea_collection_slug='pudgy-penguins',
        okx_max_pages_per_run=5,
        opensea_max_pages_per_run=2,
        rules_path=tmp_path / 'rule_packs.json',
    )
    store = SQLiteStore(settings.db_path)
    monitor = Monitor(settings=settings, store=store, notifier=NullNotifier())
    fake_client = FakeOpenSeaClient(
        pages=[
            _opensea_sale_page(1, next_cursor='cursor-2'),
            _opensea_sale_page(2, next_cursor='cursor-3'),
            _opensea_sale_page(3, next_cursor='cursor-4'),
        ]
    )
    monkeypatch.setattr(live_cycle, 'OpenSeaClient', lambda settings: fake_client)

    result = monitor.run_live_cycle(source_mode='trades')

    assert result.pages_fetched == 2
    assert fake_client.event_calls == [None, 'cursor-2']
    assert result.end_cursor == 'cursor-3'
    assert len(result.new_events) == 2
    assert store.count_events() == 2


def test_monitor_stops_when_provider_has_no_next_cursor(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        app_env='test',
        db_path=tmp_path / 'db.sqlite3',
        active_market='opensea',
        opensea_collection_slug='pudgy-penguins',
        okx_max_pages_per_run=5,
        opensea_max_pages_per_run=4,
        rules_path=tmp_path / 'rule_packs.json',
    )
    store = SQLiteStore(settings.db_path)
    monitor = Monitor(settings=settings, store=store, notifier=NullNotifier())
    fake_client = FakeOpenSeaClient(
        pages=[
            _opensea_sale_page(1, next_cursor='cursor-2'),
            _opensea_sale_page(2, next_cursor=None),
        ]
    )
    monkeypatch.setattr(live_cycle, 'OpenSeaClient', lambda settings: fake_client)

    result = monitor.run_live_cycle(source_mode='trades')

    assert result.pages_fetched == 2
    assert fake_client.event_calls == [None, 'cursor-2']
    assert result.end_cursor is None
    assert len(result.new_events) == 2
