from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any
from urllib import parse

from okx_nft_bot.analytics.cross_market import detect_spreads, rank_collections
from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings
from okx_nft_bot.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)
_HEALTH_ALERT_CHANNEL = 'ops_health'
_HEALTH_ALERT_CONTROL_NAMESPACE = 'ops_health_control'
_ALERTABLE_HEALTH_REASONS = {
    'execution_integrity_issues',
    'execution_killswitch_failed',
    'execution_never_reconciled',
    'execution_reconcile_stale',
    'execution_state_invalid',
}


@dataclass(slots=True)
class HealthResult:
    healthy: bool
    reason: str
    age_seconds: float | None
    payload: dict[str, Any]


@dataclass(slots=True)
class HealthAlertResult:
    attempted: bool
    sent: bool
    reason: str
    event_id: str | None
    delivered_channels: list[str]
    errors: list[str]
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            'attempted': self.attempted,
            'sent': self.sent,
            'reason': self.reason,
            'event_id': self.event_id,
            'delivered_channels': list(self.delivered_channels),
            'errors': list(self.errors),
            'payload': self.payload,
        }


@dataclass(slots=True)
class HealthAlertControlState:
    snoozed: bool
    snooze_until: str | None
    snooze_remaining_seconds: float | None
    snoozed_by: str | None
    snooze_reason: str | None
    acknowledged_reason: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None
    acknowledged_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            'snoozed': self.snoozed,
            'snooze_until': self.snooze_until,
            'snooze_remaining_seconds': self.snooze_remaining_seconds,
            'snoozed_by': self.snoozed_by,
            'snooze_reason': self.snooze_reason,
            'acknowledged_reason': self.acknowledged_reason,
            'acknowledged_at': self.acknowledged_at,
            'acknowledged_by': self.acknowledged_by,
            'acknowledged_note': self.acknowledged_note,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_event_time(store: SQLiteStore) -> str | None:
    rows = store.fetch_latest_events(limit=1)
    if not rows:
        return None
    value = rows[0].get('event_time')
    return None if value is None else str(value)


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clear_health_alert_keys(store: SQLiteStore, *keys: str) -> None:
    for key in keys:
        store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, key, None)


def get_health_alert_control(store: SQLiteStore, *, now: datetime | None = None) -> HealthAlertControlState:
    resolved_now = (now or _utc_now()).astimezone(timezone.utc)
    snooze_until_raw = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snooze_until')
    snoozed_by = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snoozed_by')
    snooze_reason = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snooze_reason')
    acknowledged_reason = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_reason')
    acknowledged_at = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_at')
    acknowledged_by = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_by')
    acknowledged_note = store.get_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_note')

    snooze_until = _parse_iso_utc(snooze_until_raw)
    if snooze_until_raw and (snooze_until is None or snooze_until <= resolved_now):
        _clear_health_alert_keys(store, 'snooze_until', 'snoozed_by', 'snooze_reason')
        snooze_until_raw = None
        snoozed_by = None
        snooze_reason = None
        snooze_until = None

    if acknowledged_at and _parse_iso_utc(acknowledged_at) is None:
        store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_at', None)
        acknowledged_at = None

    snooze_remaining_seconds = None
    if snooze_until is not None:
        snooze_remaining_seconds = max((snooze_until - resolved_now).total_seconds(), 0.0)

    return HealthAlertControlState(
        snoozed=snooze_until is not None,
        snooze_until=snooze_until_raw,
        snooze_remaining_seconds=snooze_remaining_seconds,
        snoozed_by=snoozed_by,
        snooze_reason=snooze_reason,
        acknowledged_reason=acknowledged_reason,
        acknowledged_at=acknowledged_at,
        acknowledged_by=acknowledged_by,
        acknowledged_note=acknowledged_note,
    )


def acknowledge_health_alert(
    store: SQLiteStore,
    *,
    reason: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> HealthAlertControlState:
    resolved_now = (now or _utc_now()).astimezone(timezone.utc)
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_reason', reason)
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_at', resolved_now.isoformat())
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_by', actor)
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'acknowledged_note', note)
    return get_health_alert_control(store, now=resolved_now)


def clear_acknowledged_health_alert(
    store: SQLiteStore,
    *,
    now: datetime | None = None,
) -> HealthAlertControlState:
    _clear_health_alert_keys(store, 'acknowledged_reason', 'acknowledged_at', 'acknowledged_by', 'acknowledged_note')
    return get_health_alert_control(store, now=now)


def snooze_health_alerts(
    store: SQLiteStore,
    *,
    minutes: int,
    actor: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> HealthAlertControlState:
    resolved_now = (now or _utc_now()).astimezone(timezone.utc)
    resolved_minutes = max(int(minutes), 1)
    snooze_until = resolved_now + timedelta(minutes=resolved_minutes)
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snooze_until', snooze_until.isoformat())
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snoozed_by', actor)
    store.set_state(_HEALTH_ALERT_CONTROL_NAMESPACE, 'snooze_reason', reason)
    return get_health_alert_control(store, now=resolved_now)


def reset_health_alert_control(
    store: SQLiteStore,
    *,
    now: datetime | None = None,
) -> HealthAlertControlState:
    _clear_health_alert_keys(
        store,
        'snooze_until',
        'snoozed_by',
        'snooze_reason',
        'acknowledged_reason',
        'acknowledged_at',
        'acknowledged_by',
        'acknowledged_note',
    )
    return get_health_alert_control(store, now=now)


def is_alertable_health_result(result: HealthResult) -> bool:
    return (not result.healthy) and result.reason in _ALERTABLE_HEALTH_REASONS


def _build_execution_snapshot(settings: Settings) -> dict[str, Any]:
    from okx_nft_bot.undercutter.state import PositionState

    payload: dict[str, Any] = {
        'db_path': str(settings.execution_db_path),
        'db_exists': settings.execution_db_path.exists(),
        'chain': settings.execution_chain,
        'configured_dry_run': settings.dry_run,
    }
    if not settings.execution_db_path.exists():
        return payload

    try:
        state = PositionState(settings.execution_db_path)
        integrity = state.audit_integrity().to_dict()
        runtime = state.get_runtime_state()
        now = _utc_now()
        last_reconcile_at = runtime.get('last_reconcile_at')
        reconcile_at = _parse_iso_utc(last_reconcile_at)
        active_offers = state.get_active_offers(chain=settings.execution_chain)
        live_active_offer_count = sum(1 for offer in active_offers if not offer.order_hash.startswith('dryrun-'))
        dry_run_active_offer_count = sum(1 for offer in active_offers if offer.order_hash.startswith('dryrun-'))
        payload.update(
            {
                'effective_dry_run': state.effective_dry_run(settings.dry_run),
                'forced_dry_run': state.is_force_dry_run(),
                'integrity': integrity,
                'live_arm': state.get_live_arm_state(now=now),
                'active_offer_count': len(active_offers),
                'live_active_offer_count': live_active_offer_count,
                'dry_run_active_offer_count': dry_run_active_offer_count,
                'killswitch_failed_count': len(state.get_killswitch_failed_offers(chain=settings.execution_chain)),
                'last_reconcile_at': last_reconcile_at,
                'reconcile_age_seconds': (
                    max((now - reconcile_at).total_seconds(), 0.0) if reconcile_at is not None else None
                ),
            }
        )
    except Exception as exc:  # pragma: no cover
        payload['error'] = repr(exc)
    return payload


def build_runtime_metrics(settings: Settings, store: SQLiteStore, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    analysis_rows = store.fetch_analysis_events(limit=2000)
    spreads = detect_spreads(analysis_rows, min_spread_pct=3.0, top_n=5)
    rankings = rank_collections(analysis_rows, min_spread_pct=3.0, top_n=5)
    payload: dict[str, Any] = {
        'version': 1,
        'generated_at': _utc_now().isoformat(),
        'app_env': settings.app_env,
        'active_market': settings.active_market,
        'db_path': str(settings.db_path),
        'metrics_path': str(settings.metrics_path),
        'event_count': store.count_events(),
        'notification_count': store.count_notifications(),
        'state_row_count': len(store.fetch_state_rows()),
        'latest_event_time': _latest_event_time(store),
        'scheduler_interval_seconds': settings.scheduler_interval_seconds,
        'health_max_staleness_seconds': settings.health_max_staleness_seconds,
        'okx_cursor_namespace': settings.okx_cursor_namespace,
        'opensea_cursor_namespace': settings.opensea_cursor_namespace,
        'market_summary': store.fetch_market_summary(),
        'execution': _build_execution_snapshot(settings),
        'health_alerts': get_health_alert_control(store).to_dict(),
        'analytics': {
            'spread_count': len(spreads),
            'top_spread_pct': spreads[0].spread_pct if spreads else 0.0,
            'top_spread_collection': spreads[0].collection_name if spreads else None,
            'top_collection_score': rankings[0].score if rankings else 0.0,
            'top_collection_name': rankings[0].collection_name if rankings else None,
        },
    }
    if extra:
        payload.update(extra)
    return payload


def write_runtime_metrics(settings: Settings, store: SQLiteStore, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = build_runtime_metrics(settings, store, extra=extra)
    settings.metrics_path.parent.mkdir(parents=True, exist_ok=True)
    settings.metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def read_runtime_metrics(metrics_path: Path) -> dict[str, Any]:
    return json.loads(metrics_path.read_text(encoding='utf-8'))


def run_healthcheck(settings: Settings, store: SQLiteStore, *, require_fresh_metrics: bool = True) -> HealthResult:
    payload: dict[str, Any] = {
        'db_exists': settings.db_path.exists(),
        'metrics_exists': settings.metrics_path.exists(),
        'db_path': str(settings.db_path),
        'metrics_path': str(settings.metrics_path),
        'execution': _build_execution_snapshot(settings),
        'health_alerts': get_health_alert_control(store).to_dict(),
    }
    if not settings.db_path.exists():
        return HealthResult(False, 'db_missing', None, payload)

    execution = payload['execution']
    if execution.get('error'):
        return HealthResult(False, 'execution_state_invalid', None, payload)
    integrity = execution.get('integrity')
    if isinstance(integrity, dict) and int(integrity.get('issue_count', 0) or 0) > 0:
        return HealthResult(False, 'execution_integrity_issues', None, payload)
    if int(execution.get('killswitch_failed_count', 0) or 0) > 0:
        return HealthResult(False, 'execution_killswitch_failed', None, payload)
    live_active_offer_count = int(execution.get('live_active_offer_count', 0) or 0)
    reconcile_age_seconds = execution.get('reconcile_age_seconds')
    if live_active_offer_count > 0:
        if not execution.get('last_reconcile_at'):
            return HealthResult(False, 'execution_never_reconciled', None, payload)
        if isinstance(reconcile_age_seconds, (int, float)) and reconcile_age_seconds > settings.execution_reconcile_max_staleness_seconds:
            return HealthResult(False, 'execution_reconcile_stale', float(reconcile_age_seconds), payload)

    if not require_fresh_metrics:
        return HealthResult(True, 'db_only', None, payload)

    if not settings.metrics_path.exists():
        return HealthResult(False, 'metrics_missing', None, payload)

    try:
        metrics = read_runtime_metrics(settings.metrics_path)
        generated_at = datetime.fromisoformat(metrics['generated_at'])
        age_seconds = max((_utc_now() - generated_at).total_seconds(), 0.0)
        payload['metrics'] = metrics
        payload['age_seconds'] = age_seconds
    except Exception as exc:  # pragma: no cover
        payload['error'] = repr(exc)
        return HealthResult(False, 'metrics_invalid', None, payload)

    if age_seconds > settings.health_max_staleness_seconds:
        return HealthResult(False, 'metrics_stale', age_seconds, payload)
    return HealthResult(True, 'ok', age_seconds, payload)


def maybe_send_health_alert(
    settings: Settings,
    *,
    store: SQLiteStore | None = None,
    result: HealthResult | None = None,
    source: str,
    now: datetime | None = None,
    transport: StdlibHttpTransport | None = None,
) -> HealthAlertResult:
    resolved_now = (now or _utc_now()).astimezone(timezone.utc)
    resolved_store = store or SQLiteStore(settings.db_path)
    resolved_result = result or run_healthcheck(settings, resolved_store)
    control = get_health_alert_control(resolved_store, now=resolved_now)
    if control.acknowledged_reason and (
        resolved_result.healthy or resolved_result.reason != control.acknowledged_reason
    ):
        control = clear_acknowledged_health_alert(resolved_store, now=resolved_now)
    payload = {
        'healthy': resolved_result.healthy,
        'reason': resolved_result.reason,
        'age_seconds': resolved_result.age_seconds,
        'source': source,
        'generated_at': resolved_now.isoformat(),
        'execution': resolved_result.payload.get('execution', {}),
        'health_alerts': control.to_dict(),
    }
    if resolved_result.healthy or resolved_result.reason not in _ALERTABLE_HEALTH_REASONS:
        return HealthAlertResult(
            attempted=False,
            sent=False,
            reason='not_alertable',
            event_id=None,
            delivered_channels=[],
            errors=[],
            payload=payload,
        )
    if control.snoozed:
        return HealthAlertResult(
            attempted=True,
            sent=False,
            reason='snoozed',
            event_id=None,
            delivered_channels=[],
            errors=[],
            payload=payload,
        )
    if control.acknowledged_reason == resolved_result.reason:
        return HealthAlertResult(
            attempted=True,
            sent=False,
            reason='acknowledged',
            event_id=None,
            delivered_channels=[],
            errors=[],
            payload=payload,
        )
    if not ((settings.telegram_bot_token and settings.telegram_chat_id) or settings.webhook_url):
        return HealthAlertResult(
            attempted=False,
            sent=False,
            reason='no_channels',
            event_id=None,
            delivered_channels=[],
            errors=[],
            payload=payload,
        )

    cooldown_seconds = max(int(settings.health_alert_cooldown_seconds), 60)
    bucket = int(resolved_now.timestamp() // cooldown_seconds)
    event_id = f'{resolved_result.reason}:{bucket}'
    if resolved_store.was_notified(_HEALTH_ALERT_CHANNEL, event_id):
        return HealthAlertResult(
            attempted=True,
            sent=False,
            reason='deduped',
            event_id=event_id,
            delivered_channels=[],
            errors=[],
            payload=payload,
        )

    resolved_transport = transport or StdlibHttpTransport(
        timeout=settings.okx_request_timeout,
        max_retries=settings.okx_max_retries,
        rate_limit_per_sec=settings.okx_rate_limit_per_sec,
    )
    delivered_channels: list[str] = []
    errors: list[str] = []
    alert_text = _format_health_alert_text(resolved_result, source=source)

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            _send_telegram_health_alert(
                transport=resolved_transport,
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                text=alert_text,
            )
            delivered_channels.append('telegram')
        except Exception as exc:  # pragma: no cover
            logger.warning("Health alert telegram delivery failed: %s", exc)
            errors.append(f'telegram:{exc}')

    if settings.webhook_url:
        try:
            _send_webhook_health_alert(
                transport=resolved_transport,
                webhook_url=settings.webhook_url,
                payload={
                    'kind': 'ops_health_alert',
                    **payload,
                },
            )
            delivered_channels.append('webhook')
        except Exception as exc:  # pragma: no cover
            logger.warning("Health alert webhook delivery failed: %s", exc)
            errors.append(f'webhook:{exc}')

    if delivered_channels:
        resolved_store.mark_notified(
            _HEALTH_ALERT_CHANNEL,
            event_id,
            payload={
                **payload,
                'event_id': event_id,
                'delivered_channels': delivered_channels,
            },
        )
        return HealthAlertResult(
            attempted=True,
            sent=True,
            reason=resolved_result.reason,
            event_id=event_id,
            delivered_channels=delivered_channels,
            errors=errors,
            payload=payload,
        )
    return HealthAlertResult(
        attempted=True,
        sent=False,
        reason='delivery_failed',
        event_id=event_id,
        delivered_channels=[],
        errors=errors,
        payload=payload,
    )


def _format_health_alert_text(result: HealthResult, *, source: str) -> str:
    execution = result.payload.get('execution', {})
    integrity = execution.get('integrity', {})
    lines = [
        'NFT Bot Health Alert',
        f'source={source}',
        f'healthy={result.healthy}',
        f'reason={result.reason}',
    ]
    if result.age_seconds is not None:
        lines.append(f'age_seconds={result.age_seconds:.1f}')
    lines.extend(
        [
            f"execution_active_offers={execution.get('active_offer_count', 0)}",
            f"execution_live_active_offers={execution.get('live_active_offer_count', 0)}",
            f"execution_killswitch_failed={execution.get('killswitch_failed_count', 0)}",
            f"execution_integrity_issues={integrity.get('issue_count', 0)}",
            f"execution_reconcile_age_seconds={execution.get('reconcile_age_seconds')}",
        ]
    )
    return '\n'.join(lines)


def _send_telegram_health_alert(
    *,
    transport: StdlibHttpTransport,
    bot_token: str,
    chat_id: str,
    text: str,
) -> None:
    body = parse.urlencode({'chat_id': chat_id, 'text': text})
    transport.request_json(
        method='POST',
        url=f'https://api.telegram.org/bot{bot_token}/sendMessage',
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        body=body,
    )


def _send_webhook_health_alert(
    *,
    transport: StdlibHttpTransport,
    webhook_url: str,
    payload: dict[str, Any],
) -> None:
    transport.request_json(
        method='POST',
        url=webhook_url,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        body=json.dumps(payload, ensure_ascii=False),
    )
