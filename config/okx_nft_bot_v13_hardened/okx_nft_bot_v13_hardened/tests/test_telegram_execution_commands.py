from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.engine import BatchResult, CounterBidTask
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.offers_store import OffersStore
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramBotClient, TelegramCommandProcessor
from okx_nft_bot.undercutter.state import PositionState


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
    monkeypatch.setenv('OFFERS_DB_PATH', str(tmp_path / 'offers.sqlite3'))
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


def test_help_lists_execution_commands(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/help')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert '/counterrun <collection>' in text
    assert '/counterconfig <collection> <min_price> <max_price> [margin]' in text
    assert '/undercutstatus' in text
    assert '/dashboard' in text
    assert '/armlive [minutes] [reason]' in text
    assert '/disarmlive [reason]' in text
    assert '/killswitch' in text
    assert '/alertstatus - show health alert ack/snooze state' in text
    assert '/alertack [note] - acknowledge the current alertable health issue' in text
    assert '/alertsnooze [minutes] [reason] - suppress health alerts temporarily' in text
    assert '/alertreset - clear health alert ack/snooze state' in text


def test_counterrun_returns_scan_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/counterrun 0xabc')
    fake_result = BatchResult(
        chain='bsc',
        tasks=[
            CounterBidTask(
                collection='0xabc',
                chain='bsc',
                parasite_offer_bnb=0.5,
                counter_price_bnb=0.501,
                reason='Parasite detected',
                valid=True,
                parasite_maker='0xparasite',
            )
        ],
    )
    monkeypatch.setattr('okx_nft_bot.telegram_bot.CounterBidder.process_batch', lambda self, **kwargs: fake_result)

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'counterbid_scan' in text
    assert 'collection=0xabc' in text
    assert 'valid=True' in text
    assert 'parasite_maker=0xparasite' in text


def test_counterconfig_saves_execution_config(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/counterconfig 0xAbC 0.02 0.8 0.003')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'counterbid_config saved' in text
    assert 'collection=0xabc' in text
    assert 'min_price_bnb=0.020000' in text
    assert 'max_price_bnb=0.800000' in text
    assert 'margin_bnb=0.003000' in text


def test_undercutstatus_shows_active_offer_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/undercutstatus')
    monkeypatch.setattr(
        'okx_nft_bot.telegram_bot.UndercutEngine.status',
        lambda self, *, chain: {
            'chain': chain,
            'dry_run': True,
            'active_offers': [
                {'collection': '0xabc', 'price_bnb': 0.5, 'status': 'active'},
            ],
            'recent_actions': [{'action_type': 'ATTACK'}],
            'tracked_collections': [{'address': '0xabc'}],
        },
    )

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'undercut_status' in text
    assert 'active_offers=1' in text
    assert '- 0xabc @ 0.500000 BNB [active]' in text


def test_killswitch_cancels_live_and_local_offers(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/killswitch')
    state_path = Path(processor.settings.execution_db_path)

    state = PositionState(state_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.upsert_active_offer(order_hash='dryrun-1', collection='0xdef', chain='bsc', price_bnb=0.25)

    cancelled: list[str] = []

    def _cancel(self, offer_id: str, **_: object) -> bool:
        cancelled.append(offer_id)
        return True

    def _get_my_offers(self, chain: str = 'bsc', **_: object):
        return [{'offerId': 'live-offer-1'}]

    monkeypatch.setattr('okx_nft_bot.telegram_bot.OKXAPIClient.cancel_offer', _cancel)
    monkeypatch.setattr('okx_nft_bot.telegram_bot.OKXAPIClient.get_my_offers', _get_my_offers)

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'killswitch_activated' in text
    assert 'dry_run=true' in text
    assert 'exchange_seen=1' in text
    assert 'live_cancelled=1' in text
    assert 'local_cancelled=1' in text
    assert cancelled == ['live-offer-1']
    refreshed = PositionState(state_path)
    assert refreshed.is_force_dry_run() is True


def test_dashboard_shows_forced_dry_run_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/dashboard')
    processor.settings.parasite_wallets = ('0xparasite',)

    state = PositionState(Path(processor.settings.execution_db_path))
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.log_action(
        action_type='LIVE_ATTACK',
        collection='0xabc',
        chain='bsc',
        order_hash='live-offer-1',
        old_price_bnb=None,
        new_price_bnb=0.5,
        reason='seed attack',
        executed=True,
    )
    state.log_action(
        action_type='LIVE_WITHDRAW',
        collection='0xabc',
        chain='bsc',
        order_hash='live-offer-1',
        old_price_bnb=0.5,
        new_price_bnb=None,
        reason='seed withdraw',
        executed=True,
    )
    state.record_submit_event(
        engine='undercutter',
        action_type='LIVE_ATTACK',
        collection='0xabc',
        chain='bsc',
        price_bnb=0.5,
        status='submitted',
        reason='seed submit',
    )
    state.set_force_dry_run(True, reason='test')
    offers = OffersStore(Path(processor.settings.offers_db_path))
    offers.upsert_offers(
        [
            NormalizedOffer(
                market='okx',
                collection_slug_or_address='0xabc',
                chain='bsc',
                offer_id='o1',
                maker='0xparasite',
                price=0.5,
                currency='WBNB',
                quantity=1,
                status='active',
                raw_payload_hash='hash-o1',
                source_type='collection_offer',
                source_reliability='high',
            )
        ]
    )

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'NFT Bot Dashboard' in text
    assert 'Mode:          DRY-RUN (FORCED)' in text
    assert 'Integrity:     OK' in text
    assert 'Active Offers: 1' in text
    assert 'Today Actions: 2 (1 attacks, 1 withdraws)' in text
    assert 'BNB Spent:     0.5000 / 5.0000 limit' in text
    assert 'Rate:          1 / 10 per hour' in text
    assert 'Collections:   1 tracked' in text
    assert 'Parasites:     1 detected' in text


def test_armlive_and_disarmlive_commands_roundtrip(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/armlive 20 testing')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'live_armed' in text
    assert 'armed=True' in text
    assert 'minutes=20' in text

    fake_transport.command = '/disarmlive done'
    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'live_disarmed' in text
    assert 'armed=False' in text


def test_dashboard_shows_live_mode_when_not_forced(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/dashboard')
    processor.settings.dry_run = False

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'Mode:          LIVE (UNARMED BLOCKED)' in text
    assert 'Integrity:     OK' in text


def test_dashboard_surfaces_integrity_issues(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/dashboard')
    state = PositionState(Path(processor.settings.execution_db_path))
    state.set_runtime_value('live_armed_until', 'bad-ts')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'Integrity:     issues=' in text


def test_health_reports_execution_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/health')
    state = PositionState(Path(processor.settings.execution_db_path))
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'execution_db=True' in text
    assert 'active_offers=1' in text
    assert 'integrity_issues=0' in text
    assert 'alerts=ACTIVE' in text


def test_writemetrics_reports_execution_summary(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/writemetrics')
    state = PositionState(Path(processor.settings.execution_db_path))
    state.set_runtime_value('live_armed_until', 'bad-ts')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'metrics_written' in text
    assert 'execution_integrity_issues=' in text
    assert 'alerts=ACTIVE' in text


def test_parasitelive_on_is_blocked_and_stays_dry_run(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/parasitelive on')

    class DummyHunter:
        def __init__(self) -> None:
            self.dry_run = True

    processor.parasite_hunter = DummyHunter()

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'BLOCKED' in text
    assert 'deprecated' in text
    assert processor.parasite_hunter.dry_run is True


def test_alertack_acknowledges_current_issue(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/alertack operator_seen')
    state = PositionState(Path(processor.settings.execution_db_path))
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.mark_offer_status(order_hash='live-offer-1', status='killswitch_failed')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'alert_ack' in text
    assert 'acknowledged=True' in text
    assert 'health_reason=execution_killswitch_failed' in text
    assert 'alerts=ACK execution_killswitch_failed' in text


def test_alertsnooze_and_reset_commands_roundtrip(tmp_path: Path, monkeypatch) -> None:
    fake_transport, processor = _build_processor(tmp_path, monkeypatch, '/alertsnooze 45 maintenance')

    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'alert_snooze' in text
    assert 'snoozed=True' in text
    assert 'minutes=45' in text
    assert 'alerts=SNOOZED' in text

    fake_transport.command = '/alertstatus'
    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'alert_status' in text
    assert 'alerts=SNOOZED' in text

    fake_transport.command = '/alertreset'
    result = processor.poll_once()

    assert result['processed'] == 1
    payload = parse_qs(fake_transport.calls[-1]['body'])
    text = payload['text'][0]
    assert 'alert_reset' in text
    assert 'reset=True' in text
    assert 'alerts=ACTIVE' in text
