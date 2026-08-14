from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.models import DeliveryResult, FilterDecision, RawEvent
from okx_nft_bot.pipeline import live_cycle as live_cycle_module
from okx_nft_bot.pipeline.live_cycle import Monitor
from okx_nft_bot.storage.sqlite import SQLiteStore


NAMESPACE = "opensea:trades:r73"
EVENT_ID = "evt-r73"


def _raw_event() -> RawEvent:
    return RawEvent(
        source="r73-test",
        payload={
            "event_id": EVENT_ID,
            "market": "opensea",
            "event_type": "sale",
            "collection": "collection-r73",
            "token_id": "1",
            "price": 1.0,
            "event_time": "2026-08-14T00:00:00Z",
        },
    )


class _SinglePageSource:
    def __init__(self, event: RawEvent) -> None:
        self.event = event

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        assert cursor == "cursor-0"
        return {"events": [self.event], "next_cursor": "cursor-1"}


class _SuccessNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        active_market="opensea",
        opensea_cursor_namespace="r73",
        opensea_max_pages_per_run=1,
        rules_path="unused",
        notification_mode="all",
    )


def _wire(monkeypatch, raw: RawEvent) -> None:
    monkeypatch.setattr(Monitor, "_build_source", lambda self, source_mode: _SinglePageSource(raw))
    monkeypatch.setattr(live_cycle_module, "load_rule_packs", lambda path: [])
    monkeypatch.setattr(
        live_cycle_module,
        "evaluate_event",
        lambda event, settings, rule_packs: FilterDecision(event_id=event.event_id, passed=True),
    )


def test_restart_does_not_replay_ambiguous_success_after_receipt_crash(monkeypatch, tmp_path) -> None:
    raw = _raw_event()
    db_path = tmp_path / "r73.sqlite3"
    first_store = SQLiteStore(db_path)
    first_store.set_state(NAMESPACE, "cursor", "cursor-0")
    first_notifier = _SuccessNotifier()
    first_monitor = Monitor(settings=_settings(), store=first_store, notifier=first_notifier)
    _wire(monkeypatch, raw)

    def _crash_before_receipt(channel: str, event_id: str, payload=None) -> None:
        raise RuntimeError("simulated crash after external send")

    monkeypatch.setattr(first_store, "mark_notified", _crash_before_receipt)

    with pytest.raises(RuntimeError, match="simulated crash after external send"):
        first_monitor.run_live_cycle()

    assert first_notifier.calls == 1
    assert first_store.get_state(NAMESPACE, "cursor") == "cursor-0"

    second_store = SQLiteStore(db_path)
    second_notifier = _SuccessNotifier()
    second_monitor = Monitor(settings=_settings(), store=second_store, notifier=second_notifier)

    result = second_monitor.run_live_cycle()

    assert second_notifier.calls == 0
    assert result.deliveries[0].delivered is False
    assert result.deliveries[0].detail == "delivery_outcome_unknown"
    assert second_store.get_state(NAMESPACE, "cursor") == "cursor-0"
    assert result.end_cursor == "cursor-0"
