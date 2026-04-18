from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from okx_nft_bot.config import load_settings
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.offers_store import OffersStore
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


def _offer(*, offer_id: str, market: str, collection: str, token_id: str | None) -> NormalizedOffer:
    return NormalizedOffer(
        market=market,
        collection_slug_or_address=collection,
        chain='ethereum' if market == 'opensea' else 'eth',
        token_id=token_id,
        offer_id=offer_id,
        maker='0xmaker',
        price=1.25 if market == 'opensea' else 0.75,
        currency='ETH',
        quantity=1,
        status='active',
        raw_payload_hash=f'hash-{offer_id}',
        observed_at=datetime.now(timezone.utc),
        source_type='token_offer' if token_id else 'collection_offer',
        source_reliability='high',
    )


def _build_processor(
    tmp_path: Path,
    monkeypatch,
    command: str,
    *,
    offers: list[NormalizedOffer] | None = None,
) -> tuple[FakeTransport, TelegramCommandProcessor]:
    registry_path = tmp_path / 'registry.json'
    registry_path.write_text(
        '{"collections":[{"name":"alpha","market":"okx","collection_address":"0xabc","enabled":true,"source_modes":["trades"]}]}',
        encoding='utf-8',
    )
    offers_db_path = tmp_path / 'offers.sqlite3'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REGISTRY_PATH', str(registry_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('OFFERS_DB_PATH', str(offers_db_path))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    monkeypatch.setenv('TELEGRAM_ADMIN_CHAT_IDS', '123')

    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(settings.registry_path)
    offers_store = OffersStore(settings.offers_db_path)
    offers_store.upsert_offers(offers or [])

    fake_transport = FakeTransport(command)
    client = TelegramBotClient(bot_token='token', transport=fake_transport)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    processor = TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=client)
    return fake_transport, processor


def test_offers_command_returns_market_rows(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers okx 2',
        offers=[
            _offer(offer_id='okx-1', market='okx', collection='0xokxcollection', token_id='1'),
            _offer(offer_id='os-1', market='opensea', collection='pudgy-penguins', token_id='22'),
        ],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'Stored offers for okx' in text
    assert '0xokxcollection #1' in text
    assert 'pudgy-penguins' not in text


def test_offers_command_filters_okx_collection(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers okx 0xmatch 3',
        offers=[
            _offer(offer_id='okx-1', market='okx', collection='0xmatch', token_id='1'),
            _offer(offer_id='okx-2', market='okx', collection='0xother', token_id='2'),
        ],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'Stored offers for okx collection=0xmatch' in text
    assert '0xmatch #1' in text
    assert '0xother #2' not in text


def test_offers_command_filters_opensea_slug_and_limit(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers opensea pudgy-penguins 1',
        offers=[
            _offer(offer_id='os-1', market='opensea', collection='pudgy-penguins', token_id='22'),
            _offer(offer_id='os-2', market='opensea', collection='pudgy-penguins', token_id='23'),
            _offer(offer_id='os-3', market='opensea', collection='other-slug', token_id='24'),
        ],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'Stored offers for opensea collection=pudgy-penguins' in text
    assert 'pudgy-penguins #22' in text or 'pudgy-penguins #23' in text
    assert text.count('\n- ') == 1
    assert 'other-slug' not in text


def test_offers_command_returns_empty_state_for_market(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers opensea 3',
        offers=[_offer(offer_id='okx-1', market='okx', collection='0xokxcollection', token_id='1')],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert payload['text'][0] == 'No stored offers for market=opensea'


def test_offers_command_returns_empty_state_for_market_and_collection(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers opensea pudgy-penguins 3',
        offers=[_offer(offer_id='os-1', market='opensea', collection='other-slug', token_id='22')],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert payload['text'][0] == 'No stored offers for market=opensea collection=pudgy-penguins'


def test_offers_command_rejects_unknown_market(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/offers nope')
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert payload['text'][0] == 'Unknown market. Use okx or opensea'


def test_offers_command_rejects_invalid_limit(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/offers okx 0')
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert payload['text'][0] == 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'


def test_offers_command_treats_numeric_second_arg_as_limit(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(
        tmp_path,
        monkeypatch,
        '/offers okx 1',
        offers=[
            _offer(offer_id='okx-1', market='okx', collection='0xfirst', token_id='1'),
            _offer(offer_id='okx-2', market='okx', collection='0xsecond', token_id='2'),
        ],
    )
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert text.startswith('Stored offers for okx:')
    assert text.count('\n- ') == 1


def test_offers_command_rejects_invalid_third_arg(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/offers okx 0xmatch nope')
    result = processor.poll_once()
    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    assert payload['text'][0] == 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'
