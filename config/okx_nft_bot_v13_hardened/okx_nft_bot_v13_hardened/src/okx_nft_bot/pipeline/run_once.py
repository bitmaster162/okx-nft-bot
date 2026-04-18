from __future__ import annotations

from okx_nft_bot.alerts.filters import evaluate_event
from okx_nft_bot.config import Settings
from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.pipeline.normalize import normalize_many
from okx_nft_bot.providers.base import Provider
from okx_nft_bot.storage.sqlite import SQLiteStore


class RunOnceResult:
    def __init__(self, events: list[NFTEvent], decisions: list[FilterDecision]) -> None:
        self.events = events
        self.decisions = decisions


def run_once(provider: Provider, store: SQLiteStore, settings: Settings) -> RunOnceResult:
    raw_events = provider.fetch_events()
    store.insert_raw_events(raw_events)
    events = normalize_many(raw_events)
    store.upsert_normalized_events(events)
    decisions = [evaluate_event(event, settings) for event in events]
    return RunOnceResult(events=events, decisions=decisions)
