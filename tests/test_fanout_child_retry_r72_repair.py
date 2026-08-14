from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.models import DeliveryResult, FilterDecision, RawEvent
from okx_nft_bot.notifiers.fanout import FanoutNotifier
from okx_nft_bot.pipeline import live_cycle as live_cycle_module
from okx_nft_bot.pipeline.live_cycle import Monitor


NAMESPACE = "opensea:trades:r72"
EVENT_ID = "evt-r72"


def _raw_event() -> RawEvent:
    return RawEvent(
        source="r72-test",
        payload={
            "event_id": EVENT_ID,
            "market": "opensea",
            "event_type": "sale",
            "collection": "collection-r72",
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


class _SuccessChild:
    channel = "telegram"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)


class _FlakyChild:
    channel = "webhook"

    def __init__(self) -> None:
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        if self.calls == 1:
            return DeliveryResult(
                channel=self.channel,
                event_id=alert.event.event_id,
                delivered=False,
                detail="temporary child failure",
            )
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        active_market="opensea",
        opensea_cursor_namespace="r72",
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


def test_partial_fanout_retries_only_failed_child_before_cursor_commit(monkeypatch) -> None:
    raw = _raw_event()
    store = _MemoryStore()
    success = _SuccessChild()
    flaky = _FlakyChild()
    source = _SinglePageRetrySource(raw)
    monitor = Monitor(settings=_settings(), store=store, notifier=FanoutNotifier([success, flaky]))
    _wire(monkeypatch, monitor, source)

    first = monitor.run_live_cycle()

    assert success.calls == 1
    assert flaky.calls == 1
    assert store.was_notified("telegram", EVENT_ID)
    assert not store.was_notified("webhook", EVENT_ID)
    assert store.state[(NAMESPACE, "cursor")] == "cursor-0"
    assert first.end_cursor == "cursor-0"
    assert first.deliveries[0].channel == "fanout"
    assert first.deliveries[0].delivered is False

    second = monitor.run_live_cycle()

    assert success.calls == 1
    assert flaky.calls == 2
    assert store.was_notified("telegram", EVENT_ID)
    assert store.was_notified("webhook", EVENT_ID)
    assert store.state[(NAMESPACE, "cursor")] == "cursor-1"
    assert second.end_cursor == "cursor-1"
    assert second.deliveries[0].channel == "fanout"
    assert second.deliveries[0].delivered is True


def test_legacy_aggregate_fanout_marker_does_not_suppress_child_reconciliation(monkeypatch) -> None:
    raw = _raw_event()
    store = _MemoryStore()
    store.sent.add(("fanout", EVENT_ID))
    success = _SuccessChild()
    second_success = _SuccessChild()
    second_success.channel = "webhook"
    source = _SinglePageRetrySource(raw)
    monitor = Monitor(settings=_settings(), store=store, notifier=FanoutNotifier([success, second_success]))
    _wire(monkeypatch, monitor, source)

    result = monitor.run_live_cycle()

    assert success.calls == 1
    assert second_success.calls == 1
    assert store.was_notified("telegram", EVENT_ID)
    assert store.was_notified("webhook", EVENT_ID)
    assert store.state[(NAMESPACE, "cursor")] == "cursor-1"
    assert result.end_cursor == "cursor-1"
