from pathlib import Path
from urllib.parse import parse_qs

from okx_nft_bot.config import load_settings
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramBotClient, TelegramCommandProcessor


class FakeTransport:
    def __init__(self, command: str) -> None:
        self.command = command
        self.calls = []

    def request_json(self, *, method, url, headers, body=''):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        if url.endswith('/getUpdates'):
            return {
                'ok': True,
                'result': [
                    {'update_id': 1, 'message': {'chat': {'id': 123}, 'text': self.command}}
                ],
            }
        return {'ok': True, 'result': {'message_id': 1}}


class FakeRunner(MultiCollectionRunner):
    def run_collection_once(self, target_name: str, source_mode: str = 'trades'):
        raise AssertionError('should not be called')


def _build_processor(tmp_path: Path, monkeypatch, command: str) -> tuple[SQLiteStore, FakeTransport, TelegramCommandProcessor]:
    registry_path = tmp_path / 'registry.json'
    registry_path.write_text('{"collections":[{"name":"alpha","market":"okx","collection_address":"0xabc","enabled":true,"source_modes":["trades"]}]}', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REGISTRY_PATH', str(registry_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    monkeypatch.setenv('TELEGRAM_ADMIN_CHAT_IDS', '123')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(settings.registry_path)
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
    store.upsert_normalized_events([
        NFTEvent(event_id='1', market='okx', event_type='listing', collection='Alpha', token_id='1', contract_address='0xabc', price=1.0, currency='ETH', event_time=now, volume_24h=120.0, floor_price=1.0, raw_source='x'),
        NFTEvent(event_id='2', market='opensea', event_type='listing', collection='Alpha', token_id='2', contract_address='0xabc', price=1.25, currency='ETH', event_time=now, volume_24h=80.0, floor_price=1.2, raw_source='y'),
    ])
    fake_transport = FakeTransport(command)
    client = TelegramBotClient(bot_token='token', transport=fake_transport)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    processor = TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=client)
    return store, fake_transport, processor


def test_spreads_command_returns_cross_market_report(tmp_path: Path, monkeypatch) -> None:
    _store, fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/spreads 5 3')
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert 'Cross-market spreads' in payload['text'][0]


def test_rankings_command_returns_collection_ranking(tmp_path: Path, monkeypatch) -> None:
    _store, fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/rankings 3')
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert 'Collection ranking' in payload['text'][0]
