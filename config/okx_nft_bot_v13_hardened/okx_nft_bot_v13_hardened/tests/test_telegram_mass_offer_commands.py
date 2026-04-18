from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from okx_nft_bot.config import load_settings
from okx_nft_bot.mass_offer.engine import MassOfferRunResult
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramBotClient, TelegramCommandProcessor


class FakeTransport:
    def __init__(self, command: str) -> None:
        self.command = command
        self.calls: list[dict[str, object]] = []

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


def _build_processor(tmp_path: Path, monkeypatch, command: str) -> tuple[FakeTransport, TelegramCommandProcessor]:
    registry_path = tmp_path / 'registry.json'
    registry_path.write_text(
        '{"collections":[{"name":"alpha","market":"okx","collection_address":"0xabc","enabled":true,"source_modes":["trades"]}]}',
        encoding='utf-8',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REGISTRY_PATH', str(registry_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_ADMIN_CHAT_IDS', '123')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(settings.registry_path)
    fake_transport = FakeTransport(command)
    client = TelegramBotClient(bot_token='token', transport=fake_transport)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    processor = TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=client)
    return fake_transport, processor


def test_massoffer_command_returns_run_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/massoffer 0xabc 0.01 R,SR')
    monkeypatch.setattr(
        'okx_nft_bot.telegram_bot.MassOfferEngine.run',
        lambda self, **kwargs: MassOfferRunResult(
            campaign_id=1,
            collection='0xabc',
            chain='bsc',
            price_bnb=0.01,
            duration_hours=24,
            delay_seconds=2.0,
            dry_run=True,
            scanned_count=10,
            target_count=2,
            submitted_count=0,
            dry_run_count=2,
            skipped_count=8,
            failed_count=0,
            results=[],
            skipped=[],
        ),
    )

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'mass_offer' in text
    assert 'collection=0xabc' in text
    assert 'dry_run=True' in text
    assert 'targets=2' in text


def test_massofferstatus_command_returns_snapshot(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/massofferstatus')
    monkeypatch.setattr(
        'okx_nft_bot.telegram_bot.MassOfferEngine.status',
        lambda self, **kwargs: {
            'chain': 'bsc',
            'effective_dry_run': True,
            'active_offer_count': 1,
            'active_offers': [{'token_id': 9, 'price_bnb': 0.02, 'status': 'active'}],
            'campaigns': [
                {
                    'campaign_id': 3,
                    'collection': '0xabc',
                    'status': 'completed',
                    'target_count': 4,
                    'submitted_count': 1,
                    'dry_run_count': 3,
                    'skipped_count': 5,
                    'failed_count': 0,
                }
            ],
        },
    )

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'mass_offer_status' in text
    assert 'active_offers=1' in text
    assert 'latest_campaign_id=3' in text
    assert '- #9 @ 0.020000 BNB [active]' in text


def test_massoffercancel_command_returns_cancel_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/massoffercancel')
    monkeypatch.setattr(
        'okx_nft_bot.telegram_bot.MassOfferEngine.cancel_active',
        lambda self, **kwargs: {
            'chain': 'bsc',
            'active_seen': 2,
            'cancelled': 2,
            'failed': [],
        },
    )

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'mass_offer_cancel' in text
    assert 'active_seen=2' in text
    assert 'cancelled=2' in text
