from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.models import RawEvent
from okx_nft_bot.pipeline.live_cycle import Monitor
from okx_nft_bot.pipeline.normalize import normalize_raw_event


def _raw_event(*, event_id: str, event_time: str | None = "2026-08-14T00:00:00Z") -> RawEvent:
    payload: dict[str, object] = {
        "event_id": event_id,
        "market": "opensea",
        "event_type": "sale",
        "collection": "collection-r70",
        "token_id": "1",
        "price": 1.0,
    }
    if event_time is not None:
        payload["event_time"] = event_time
    return RawEvent(source="r70-test", payload=payload)


def test_missing_event_time_is_rejected_instead_of_fabricating_freshness() -> None:
    raw = _raw_event(event_id="evt-missing-time", event_time=None)

    with pytest.raises(ValueError, match="event_time"):
        normalize_raw_event(raw)


class _SinglePageSource:
    def __init__(self, event: RawEvent) -> None:
        self.event = event

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        assert cursor == "cursor-0"
        return {"events": [self.event], "next_cursor": "cursor-1"}


class _PersistenceFailingStore:
    def __init__(self) -> None:
        self.state = {("opensea:trades:r70", "cursor"): "cursor-0"}
        self.raw_events: list[RawEvent] = []

    def get_state(self, namespace: str, key: str) -> str | None:
        return self.state.get((namespace, key))

    def set_state(self, namespace: str, key: str, value: str | None) -> None:
        self.state[(namespace, key)] = value

    def insert_raw_events(self, events: list[RawEvent]) -> None:
        self.raw_events.extend(events)

    def filter_new_events(self, events):
        return list(events)

    def upsert_normalized_events(self, events) -> None:
        raise RuntimeError("normalized persistence failed")


def test_cursor_does_not_advance_when_normalized_persistence_fails(monkeypatch) -> None:
    store = _PersistenceFailingStore()
    source = _SinglePageSource(_raw_event(event_id="evt-persist-failure"))
    settings = SimpleNamespace(
        active_market="opensea",
        opensea_cursor_namespace="r70",
        opensea_max_pages_per_run=1,
    )
    monitor = Monitor(settings=settings, store=store, notifier=SimpleNamespace(channel="test"))
    monkeypatch.setattr(Monitor, "_build_source", lambda self, source_mode: source)

    with pytest.raises(RuntimeError, match="normalized persistence failed"):
        monitor.run_live_cycle()

    assert store.state[("opensea:trades:r70", "cursor")] == "cursor-0"
