from __future__ import annotations

from dataclasses import dataclass

from okx_nft_bot.alerts.filters import evaluate_event
from okx_nft_bot.clients.magiceden import MagicEdenClient
from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import Settings
from okx_nft_bot.logging_utils import log_event
from okx_nft_bot.models import DeliveryResult, FilterDecision, NFTEvent, RawEvent
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier
from okx_nft_bot.notifiers.fanout import FanoutNotifier
from okx_nft_bot.pipeline.normalize import normalize_many
from okx_nft_bot.providers.magiceden_marketplace import MagicEdenListingsProvider, MagicEdenTradesProvider
from okx_nft_bot.providers.okx_marketplace import OKXMarketplaceListingsProvider, OKXMarketplaceTradesProvider
from okx_nft_bot.providers.opensea_marketplace import OpenSeaBestListingsProvider, OpenSeaCollectionEventsProvider
from okx_nft_bot.rules.rule_packs import load_rule_packs
from okx_nft_bot.storage.sqlite import SQLiteStore


@dataclass(slots=True)
class LiveCycleResult:
    raw_events: list[RawEvent]
    new_events: list[NFTEvent]
    decisions: list[FilterDecision]
    deliveries: list[DeliveryResult]
    start_cursor: str | None
    end_cursor: str | None
    pages_fetched: int
    source_mode: str


class CursorState:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def get(self, store: SQLiteStore) -> str | None:
        return store.get_state(self.namespace, 'cursor')

    def set(self, store: SQLiteStore, cursor: str | None) -> None:
        store.set_state(self.namespace, 'cursor', cursor)

    def reset(self, store: SQLiteStore) -> None:
        store.clear_namespace(self.namespace)


class EventSource:
    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        raise NotImplementedError


@dataclass(slots=True)
class Monitor:
    settings: Settings
    store: SQLiteStore
    notifier: Notifier

    def _build_source(self, source_mode: str) -> EventSource:
        market = self.settings.active_market
        if market == 'opensea':
            client = OpenSeaClient(self.settings)
            if source_mode == 'listings':
                return OpenSeaBestListingsProvider(client=client, settings=self.settings)
            return OpenSeaCollectionEventsProvider(client=client, settings=self.settings)
        if market == 'magiceden':
            client = MagicEdenClient(self.settings)
            if source_mode == 'listings':
                return MagicEdenListingsProvider(client=client, settings=self.settings)
            return MagicEdenTradesProvider(client=client, settings=self.settings)
        client = OKXMarketplaceClient(self.settings)
        if source_mode == 'listings':
            return OKXMarketplaceListingsProvider(client=client, settings=self.settings)
        return OKXMarketplaceTradesProvider(client=client, settings=self.settings)

    def _cursor_namespace(self, source_mode: str) -> str:
        market = self.settings.active_market
        if market == 'opensea':
            suffix = self.settings.opensea_cursor_namespace
        elif market == 'magiceden':
            suffix = self.settings.magiceden_cursor_namespace
        else:
            suffix = self.settings.okx_cursor_namespace
        return f'{market}:{source_mode}:{suffix}'

    def _max_pages_per_run(self) -> int:
        market = self.settings.active_market
        if market == 'opensea':
            return max(self.settings.opensea_max_pages_per_run, 1)
        return max(self.settings.okx_max_pages_per_run, 1)

    def run_live_cycle(self, source_mode: str = 'trades') -> LiveCycleResult:
        source = self._build_source(source_mode)
        state = CursorState(namespace=self._cursor_namespace(source_mode))
        start_cursor = state.get(self.store)
        cursor = start_cursor
        raw_events: list[RawEvent] = []
        pages_fetched = 0
        max_pages = self._max_pages_per_run()
        for page_no in range(1, max_pages + 1):
            page = source.fetch_page(cursor=cursor)
            page_events = page['events']
            next_cursor = page.get('next_cursor')
            pages_fetched = page_no
            if page_events:
                self.store.insert_raw_events(page_events)
                raw_events.extend(page_events)
            log_event(
                'market_page_fetched',
                market=self.settings.active_market,
                source_mode=source_mode,
                page_no=page_no,
                event_count=len(page_events),
                cursor_in=cursor,
                cursor_out=next_cursor,
            )
            if not next_cursor or next_cursor == cursor:
                cursor = next_cursor
                break
            cursor = str(next_cursor)

        normalized = normalize_many(raw_events)
        new_events = self.store.filter_new_events(normalized)
        if new_events:
            self.store.upsert_normalized_events(new_events)

        rule_packs = load_rule_packs(self.settings.rules_path)
        delivery_decisions = [evaluate_event(event, self.settings, rule_packs=rule_packs) for event in normalized]
        new_event_ids = {event.event_id for event in new_events}
        decisions = [decision for decision in delivery_decisions if decision.event_id in new_event_ids]
        deliveries = self._deliver(normalized, delivery_decisions)

        delivery_blocked = any(
            not result.delivered and result.detail not in {'already_notified', 'disabled'}
            for result in deliveries
        )
        end_cursor = start_cursor
        if not delivery_blocked:
            state.set(self.store, cursor)
            end_cursor = cursor

        return LiveCycleResult(
            raw_events=raw_events,
            new_events=new_events,
            decisions=decisions,
            deliveries=deliveries,
            start_cursor=start_cursor,
            end_cursor=end_cursor,
            pages_fetched=pages_fetched,
            source_mode=source_mode,
        )

    def _begin_delivery_attempt(self, channel: str, event_id: str, payload: dict[str, object]) -> bool:
        begin_attempt = getattr(self.store, 'begin_notification_attempt', None)
        if begin_attempt is None:
            return True
        return bool(begin_attempt(channel, event_id, payload=payload))

    def _clear_delivery_attempt(self, channel: str, event_id: str) -> None:
        clear_attempt = getattr(self.store, 'clear_notification_attempt', None)
        if clear_attempt is not None:
            clear_attempt(channel, event_id)

    def _deliver(self, events: list[NFTEvent], decisions: list[FilterDecision]) -> list[DeliveryResult]:
        if isinstance(self.notifier, FanoutNotifier):
            return self._deliver_fanout(events, decisions)

        decision_by_id = {decision.event_id: decision for decision in decisions}
        results: list[DeliveryResult] = []
        for event in events:
            decision = decision_by_id[event.event_id]
            if self.settings.notification_mode == 'passed_only' and not decision.passed:
                continue
            channel = self.notifier.channel
            if self.store.was_notified(channel, event.event_id):
                results.append(DeliveryResult(channel=channel, event_id=event.event_id, delivered=False, detail='already_notified'))
                continue
            alert = AlertEnvelope(event=event, decision=decision)
            payload = alert.event.model_dump(mode='json')
            if not self._begin_delivery_attempt(channel, event.event_id, payload):
                results.append(
                    DeliveryResult(
                        channel=channel,
                        event_id=event.event_id,
                        delivered=False,
                        detail='delivery_outcome_unknown',
                    )
                )
                continue
            try:
                result = self.notifier.send(alert)
            except Exception:
                self._clear_delivery_attempt(channel, event.event_id)
                raise
            if result.delivered:
                self.store.mark_notified(channel, event.event_id, payload=payload)
            else:
                self._clear_delivery_attempt(channel, event.event_id)
            results.append(result)
        return results

    def _deliver_fanout(self, events: list[NFTEvent], decisions: list[FilterDecision]) -> list[DeliveryResult]:
        decision_by_id = {decision.event_id: decision for decision in decisions}
        results: list[DeliveryResult] = []
        terminal_details = {'already_notified', 'disabled'}
        for event in events:
            decision = decision_by_id[event.event_id]
            if self.settings.notification_mode == 'passed_only' and not decision.passed:
                continue
            alert = AlertEnvelope(event=event, decision=decision)
            payload = alert.event.model_dump(mode='json')
            all_terminal = True
            details: list[str] = []
            for notifier in self.notifier.notifiers:
                channel = notifier.channel
                if self.store.was_notified(channel, event.event_id):
                    details.append(f'{channel}:already_notified')
                    continue
                if not self._begin_delivery_attempt(channel, event.event_id, payload):
                    all_terminal = False
                    details.append(f'{channel}:delivery_outcome_unknown')
                    continue
                try:
                    result = notifier.send(alert)
                except Exception:
                    self._clear_delivery_attempt(channel, event.event_id)
                    raise
                if result.delivered:
                    self.store.mark_notified(channel, event.event_id, payload=payload)
                    details.append(f'{channel}:ok')
                    continue
                self._clear_delivery_attempt(channel, event.event_id)
                if result.detail in terminal_details:
                    details.append(f'{channel}:{result.detail}')
                    continue
                all_terminal = False
                details.append(f'{channel}:fail')
            results.append(
                DeliveryResult(
                    channel=self.notifier.channel,
                    event_id=event.event_id,
                    delivered=all_terminal,
                    detail=', '.join(details) if all_terminal else 'fanout_incomplete: ' + ', '.join(details),
                )
            )
        return results
