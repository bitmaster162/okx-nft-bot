from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.models import DeliveryResult, FilterDecision, RawEvent
from okx_nft_bot.pipeline import live_cycle as live_cycle_module
from okx_nft_bot.pipeline.live_cycle import Monitor
from okx_nft_bot.pipeline.normalize import normalize_raw_event


NAMESPACE = "opensea:trades:r71"


def _raw_event(event_id: str = "evt-r71") -> RawEvent:
    return RawEvent(
        source="r71-test",
        payload={
            "event_id": event_id,
            "market": "opensea",
            "event_type": "sale",
            "collection": "collection-r71",
            "token_id": "1",
            "price": 1.0,
            "event_time": "2026-08-14T00:00:00Z",
        },
    )


class _SinglePageRetrySource:
    def __init__(self, event: RawEvent) -> None:
        self.event = event
        self.calls = 0

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        assert cursor == "cursor-0"
        self.calls += 1
        return {"events": [self.event], "next_cursor": "cursor-1"}


class _MemoryStore:
    def __init__(self) -> None:
        self.state = {(NAMESPACE, "cursor"): "cursor-0"}
        self.persisted = {}
        self.sent: set[tuple[str, str]] = set()

    def get_state(self, namespace: str, key: str) -> str | None:
        return self.state.get((namespace, key))

    def set_state(self, namespace: str, key: str, value: str | None) -> None:
        self.state[(namespace, key)] = value

    def insert_raw_events(self, events) -> None:
        return None

    def filter_new_events(self, events):
        return [event for event in events if event.event_id not in self.persisted]

    def upsert_normalized_events(self, events) -> None:
        for event in events:
            self.persisted[event.event_id] = event

    def was_notified(self, channel: str, event_id: str) -> bool:
        return (channel, event_id) in self.sent

    def mark_notified(self, channel: str, event_id: str, payload=None) -> None:
        self.sent.add((channel, event_id))


class _RaisingNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        raise RuntimeError("transient notification failure")


class _SuccessNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)


class _FalseNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        return DeliveryResult(
            channel=self.channel,
            event_id=alert.event.event_id,
            delivered=False,
            detail="temporary notification failure",
        )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        active_market="opensea",
        opensea_cursor_namespace="r71",
        opensea_max_pages_per_run=1,
        rules_path="unused",
        notification_mode="all",
    )


def _wire(monkeypatch, monitor: Monitor, source: _SinglePageRetrySource) -> None:
    monkeypatch.setattr(Monitor, "_build_source", lambda self, source_mode: source)
    monkeypatch.setattr(live_cycle_module, "load_rule_packs", lambda path: [])
    monkeypatch.setattr(
        live_cycle_module,
        "evaluate_event",
        lambda event, settings, rule_packs: FilterDecision(event_id=event.event_id, passed=True),
    )


def test_notifier_exception_does_not_commit_cursor(monkeypatch) -> None:
    raw = _raw_event()
    store = _MemoryStore()
    notifier = _RaisingNotifier()
    source = _SinglePageRetrySource(raw)
    monitor = Monitor(settings=_settings(), store=store, notifier=notifier)
    _wire(monkeypatch, monitor, source)

    with pytest.raises(RuntimeError, match="transient notification failure"):
        monitor.run_live_cycle()

    assert raw.payload["event_id"] in store.persisted
    assert store.state[(NAMESPACE, "cursor")] == "cursor-0"
    assert notifier.calls == 1


def test_persisted_but_unsent_event_is_retried_from_same_cursor(monkeypatch) -> None:
    raw = _raw_event()
    store = _MemoryStore()
    persisted = normalize_raw_event(raw)
    store.persisted[persisted.event_id] = persisted
    notifier = _SuccessNotifier()
    source = _SinglePageRetrySource(raw)
    monitor = Monitor(settings=_settings(), store=store, notifier=notifier)
    _wire(monkeypatch, monitor, source)

    result = monitor.run_live_cycle()

    assert result.new_events == []
    assert notifier.calls == 1
    assert store.was_notified("test", persisted.event_id)
    assert store.state[(NAMESPACE, "cursor")] == "cursor-1"


def test_transient_false_delivery_does_not_commit_cursor(monkeypatch) -> None:
    raw = _raw_event("evt-r71-false")
    store = _MemoryStore()
    notifier = _FalseNotifier()
    source = _SinglePageRetrySource(raw)
    monitor = Monitor(settings=_settings(), store=store, notifier=notifier)
    _wire(monkeypatch, monitor, source)

    result = monitor.run_live_cycle()

    assert notifier.calls == 1
    assert result.deliveries[0].delivered is False
    assert store.state[(NAMESPACE, "cursor")] == "cursor-0"
    assert result.end_cursor == "cursor-0"
