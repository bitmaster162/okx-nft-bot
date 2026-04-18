from pathlib import Path
from datetime import datetime, timezone
import json
import time

from okx_nft_bot.config import load_settings
from okx_nft_bot.ops import (
    HealthResult,
    acknowledge_health_alert,
    get_health_alert_control,
    maybe_send_health_alert,
    run_healthcheck,
    snooze_health_alerts,
    write_runtime_metrics,
)
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request_json(self, *, method, url, headers, body=''):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        return {'ok': True}


def test_write_runtime_metrics_creates_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.arm_live(minutes=15, actor='test', reason='metrics')
    payload = write_runtime_metrics(settings, store, extra={'daemon_status': 'test'})
    saved = json.loads(settings.metrics_path.read_text())
    assert saved['daemon_status'] == 'test'
    assert saved['db_path'] == str(settings.db_path)
    assert saved['execution']['db_exists'] is True
    assert saved['execution']['active_offer_count'] == 1
    assert saved['execution']['live_arm']['armed'] is True
    assert saved['health_alerts']['snoozed'] is False
    assert payload['version'] == 1


def test_healthcheck_detects_stale_metrics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    monkeypatch.setenv('HEALTH_MAX_STALENESS_SECONDS', '0')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    write_runtime_metrics(settings, store)
    time.sleep(0.02)
    result = run_healthcheck(settings, store)
    assert result.healthy is False
    assert result.reason == 'metrics_stale'


def test_healthcheck_can_skip_metrics_freshness(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    result = run_healthcheck(settings, store, require_fresh_metrics=False)
    assert result.healthy is True
    assert result.reason == 'db_only'
    assert 'execution' in result.payload


def test_healthcheck_detects_execution_integrity_issues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    state = PositionState(settings.execution_db_path)
    state.record_submit_event(
        engine='counterbid',
        action_type='LIVE_COUNTERBID',
        collection='0xabc',
        chain='bsc',
        price_bnb=0.4,
        status='submitted',
        reason='bad-submit-ts',
    )
    with state._connect() as conn:
        conn.execute("UPDATE execution_submit_log SET created_at = 'bad-ts' WHERE id = 1")
    write_runtime_metrics(settings, store)

    result = run_healthcheck(settings, store)

    assert result.healthy is False
    assert result.reason == 'execution_integrity_issues'
    assert result.payload['execution']['integrity']['issue_count'] >= 1


def test_healthcheck_detects_killswitch_failed_offers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.mark_offer_status(order_hash='live-offer-1', status='killswitch_failed')
    write_runtime_metrics(settings, store)

    result = run_healthcheck(settings, store)

    assert result.healthy is False
    assert result.reason == 'execution_killswitch_failed'
    assert result.payload['execution']['killswitch_failed_count'] == 1


def test_healthcheck_detects_live_offer_without_reconcile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    write_runtime_metrics(settings, store)

    result = run_healthcheck(settings, store)

    assert result.healthy is False
    assert result.reason == 'execution_never_reconciled'


def test_healthcheck_detects_stale_reconcile_for_live_offers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('EXECUTION_DB_PATH', str(tmp_path / 'execution.sqlite3'))
    monkeypatch.setenv('EXECUTION_RECONCILE_MAX_STALENESS_SECONDS', '1')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash='live-offer-1', collection='0xabc', chain='bsc', price_bnb=0.5)
    state.set_runtime_value('last_reconcile_at', '2020-01-01T00:00:00+00:00')
    write_runtime_metrics(settings, store)

    result = run_healthcheck(settings, store)

    assert result.healthy is False
    assert result.reason == 'execution_reconcile_stale'
    assert result.age_seconds is not None


def test_maybe_send_health_alert_delivers_and_dedupes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    monkeypatch.setenv('HEALTH_ALERT_COOLDOWN_SECONDS', '600')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    transport = FakeTransport()
    result = HealthResult(
        healthy=False,
        reason='execution_killswitch_failed',
        age_seconds=None,
        payload={'execution': {'active_offer_count': 1, 'live_active_offer_count': 1, 'killswitch_failed_count': 1, 'integrity': {'issue_count': 0}, 'reconcile_age_seconds': None}},
    )

    first = maybe_send_health_alert(
        settings,
        store=store,
        result=result,
        source='test',
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        transport=transport,
    )
    second = maybe_send_health_alert(
        settings,
        store=store,
        result=result,
        source='test',
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        transport=transport,
    )

    assert first.sent is True
    assert first.delivered_channels == ['telegram']
    assert second.sent is False
    assert second.reason == 'deduped'
    assert len(transport.calls) == 1


def test_maybe_send_health_alert_respects_snooze(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    transport = FakeTransport()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snooze_health_alerts(store, minutes=30, actor='test', reason='maintenance', now=now)
    result = HealthResult(
        healthy=False,
        reason='execution_killswitch_failed',
        age_seconds=None,
        payload={'execution': {'active_offer_count': 1, 'live_active_offer_count': 1, 'killswitch_failed_count': 1, 'integrity': {'issue_count': 0}, 'reconcile_age_seconds': None}},
    )

    alert = maybe_send_health_alert(
        settings,
        store=store,
        result=result,
        source='test',
        now=now,
        transport=transport,
    )

    assert alert.sent is False
    assert alert.reason == 'snoozed'
    assert alert.payload['health_alerts']['snoozed'] is True
    assert transport.calls == []


def test_maybe_send_health_alert_respects_acknowledged_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    transport = FakeTransport()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    acknowledge_health_alert(store, reason='execution_killswitch_failed', actor='test', note='seen', now=now)
    result = HealthResult(
        healthy=False,
        reason='execution_killswitch_failed',
        age_seconds=None,
        payload={'execution': {'active_offer_count': 1, 'live_active_offer_count': 1, 'killswitch_failed_count': 1, 'integrity': {'issue_count': 0}, 'reconcile_age_seconds': None}},
    )

    alert = maybe_send_health_alert(
        settings,
        store=store,
        result=result,
        source='test',
        now=now,
        transport=transport,
    )

    assert alert.sent is False
    assert alert.reason == 'acknowledged'
    assert alert.payload['health_alerts']['acknowledged_reason'] == 'execution_killswitch_failed'
    assert transport.calls == []


def test_maybe_send_health_alert_clears_ack_after_recovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('METRICS_PATH', str(tmp_path / 'runtime_metrics.json'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    acknowledge_health_alert(store, reason='execution_killswitch_failed', actor='test', note='seen', now=now)

    alert = maybe_send_health_alert(
        settings,
        store=store,
        result=HealthResult(healthy=True, reason='ok', age_seconds=0.0, payload={'execution': {}}),
        source='test',
        now=now,
        transport=FakeTransport(),
    )

    assert alert.sent is False
    assert alert.reason == 'not_alertable'
    assert get_health_alert_control(store).acknowledged_reason is None
