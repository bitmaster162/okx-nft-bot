from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.models import FilterDecision, RawEvent
from okx_nft_bot.notifiers.fanout import FanoutNotifier
from okx_nft_bot.pipeline import live_cycle as live_cycle_module
from okx_nft_bot.pipeline.live_cycle import Monitor
from okx_nft_bot.storage.sqlite import SQLiteStore


NAMESPACE = "opensea:trades:r83"
EVENT_ID = "evt-r83"


def _raw_event() -> RawEvent:
    return RawEvent(
        source="r83-test",
        payload={
            "event_id": EVENT_ID,
            "market": "opensea",
            "event_type": "sale",
            "collection": "collection-r83",
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


class _ExceptionNotifier:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        raise RuntimeError("simulated transport outcome unknown")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        active_market="opensea",
        opensea_cursor_namespace="r83",
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


def _assert_active_claim_is_retained(store: SQLiteStore, channel: str) -> None:
    rows = store.fetch_notification_attempts(channel=channel, event_id=EVENT_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "active"
    assert store.begin_notification_attempt(channel, EVENT_ID, payload={"retry": True}) is False


def test_single_notifier_exception_retains_active_claim(monkeypatch, tmp_path) -> None:
    raw = _raw_event()
    store = SQLiteStore(tmp_path / "r83-single.sqlite3")
    store.set_state(NAMESPACE, "cursor", "cursor-0")
    notifier = _ExceptionNotifier("single-r83")
    monitor = Monitor(settings=_settings(), store=store, notifier=notifier)
    _wire(monkeypatch, raw)

    with pytest.raises(RuntimeError, match="simulated transport outcome unknown"):
        monitor.run_live_cycle()

    assert notifier.calls == 1
    assert store.get_state(NAMESPACE, "cursor") == "cursor-0"
    _assert_active_claim_is_retained(store, notifier.channel)


def test_fanout_child_exception_retains_active_claim(monkeypatch, tmp_path) -> None:
    raw = _raw_event()
    store = SQLiteStore(tmp_path / "r83-fanout.sqlite3")
    store.set_state(NAMESPACE, "cursor", "cursor-0")
    child = _ExceptionNotifier("fanout-child-r83")
    monitor = Monitor(settings=_settings(), store=store, notifier=FanoutNotifier([child]))
    _wire(monkeypatch, raw)

    with pytest.raises(RuntimeError, match="simulated transport outcome unknown"):
        monitor.run_live_cycle()

    assert child.calls == 1
    assert store.get_state(NAMESPACE, "cursor") == "cursor-0"
    _assert_active_claim_is_retained(store, child.channel)
