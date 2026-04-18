from __future__ import annotations

from dataclasses import dataclass, replace
import time

from okx_nft_bot.config import Settings
from okx_nft_bot.logging_utils import log_event
from okx_nft_bot.notifiers.base import Notifier
from okx_nft_bot.ops import maybe_send_health_alert, write_runtime_metrics
from okx_nft_bot.pipeline.live_cycle import LiveCycleResult, Monitor
from okx_nft_bot.registry import CollectionRegistry, CollectionTarget
from okx_nft_bot.storage.sqlite import SQLiteStore


@dataclass(slots=True)
class CollectionRunResult:
    target_name: str
    source_mode: str
    result: LiveCycleResult


@dataclass(slots=True)
class DaemonSummary:
    cycles: int
    total_runs: int
    total_new_events: int
    total_deliveries: int


class MultiCollectionRunner:
    def __init__(self, *, settings: Settings, store: SQLiteStore, notifier: Notifier, registry: CollectionRegistry) -> None:
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.registry = registry

    def _settings_for_target(self, target: CollectionTarget) -> Settings:
        return replace(
            self.settings,
            active_market=target.market,
            okx_chain=target.chain if target.market == 'okx' else self.settings.okx_chain,
            okx_collection_address=target.collection_address if target.market == 'okx' else self.settings.okx_collection_address,
            okx_collection_slug=target.collection_slug if target.market == 'okx' else self.settings.okx_collection_slug,
            okx_platform=target.platform if target.market == 'okx' else self.settings.okx_platform,
            okx_cursor_namespace=target.name if target.market == 'okx' else self.settings.okx_cursor_namespace,
            opensea_chain=target.chain if target.market == 'opensea' else self.settings.opensea_chain,
            opensea_collection_slug=target.collection_slug if target.market == 'opensea' else self.settings.opensea_collection_slug,
            opensea_cursor_namespace=target.name if target.market == 'opensea' else self.settings.opensea_cursor_namespace,
            magiceden_chain=target.chain if target.market == 'magiceden' else self.settings.magiceden_chain,
            magiceden_collection_address=target.collection_address if target.market == 'magiceden' else self.settings.magiceden_collection_address,
            magiceden_collection_slug=target.collection_slug if target.market == 'magiceden' else self.settings.magiceden_collection_slug,
            magiceden_cursor_namespace=target.name if target.market == 'magiceden' else self.settings.magiceden_cursor_namespace,
            min_price=target.min_price if target.min_price is not None else self.settings.min_price,
            min_volume=target.min_volume if target.min_volume is not None else self.settings.min_volume,
        )

    def run_collection_once(self, target_name: str, source_mode: str = 'trades') -> CollectionRunResult:
        target = self.registry.get(target_name)
        if target is None:
            raise KeyError(f'Unknown collection: {target_name}')
        settings = self._settings_for_target(target)
        monitor = Monitor(settings=settings, store=self.store, notifier=self.notifier)
        result = monitor.run_live_cycle(source_mode=source_mode)
        return CollectionRunResult(target_name=target_name, source_mode=source_mode, result=result)

    def run_all_once(self) -> list[CollectionRunResult]:
        outputs: list[CollectionRunResult] = []
        for target in self.registry.active():
            for source_mode in target.source_modes:
                log_event('scheduler_dispatch', target=target.name, market=target.market, source_mode=source_mode)
                try:
                    outputs.append(self.run_collection_once(target.name, source_mode=source_mode))
                except Exception as exc:
                    log_event('scheduler_collection_error', target=target.name, market=target.market, source_mode=source_mode, error=str(exc))
        return outputs

    def run_daemon(self, *, interval_seconds: int, max_cycles: int | None = None) -> DaemonSummary:
        cycles = 0
        total_runs = 0
        total_new_events = 0
        total_deliveries = 0
        write_runtime_metrics(
            self.settings,
            self.store,
            extra={
                'daemon_status': 'starting',
                'cycles': cycles,
                'total_runs': total_runs,
                'total_new_events': total_new_events,
                'total_deliveries': total_deliveries,
            },
        )
        if self.settings.telegram_bot_token or self.settings.webhook_url:
            maybe_send_health_alert(self.settings, store=self.store, source='main_daemon')
        while True:
            cycles += 1
            cycle_results = self.run_all_once()
            total_runs += len(cycle_results)
            total_new_events += sum(len(item.result.new_events) for item in cycle_results)
            total_deliveries += sum(sum(1 for delivery in item.result.deliveries if delivery.delivered) for item in cycle_results)
            write_runtime_metrics(
                self.settings,
                self.store,
                extra={
                    'daemon_status': 'running',
                    'cycles': cycles,
                    'total_runs': total_runs,
                    'total_new_events': total_new_events,
                    'total_deliveries': total_deliveries,
                    'last_cycle_collection_runs': len(cycle_results),
                },
            )
            if self.settings.telegram_bot_token or self.settings.webhook_url:
                maybe_send_health_alert(self.settings, store=self.store, source='main_daemon')
            log_event(
                'scheduler_cycle_complete',
                cycles=cycles,
                total_runs=total_runs,
                total_new_events=total_new_events,
                total_deliveries=total_deliveries,
            )
            if max_cycles is not None and cycles >= max_cycles:
                write_runtime_metrics(
                    self.settings,
                    self.store,
                    extra={
                        'daemon_status': 'completed',
                        'cycles': cycles,
                        'total_runs': total_runs,
                        'total_new_events': total_new_events,
                        'total_deduped': 0,
                    },
                )
                break
            # Wait between cycles to avoid hammering the API
            if interval_seconds > 0:
                time.sleep(interval_seconds)
        return DaemonSummary(
            cycles=cycles,
            total_runs=total_runs,
            total_new_events=total_new_events,
            total_deliveries=total_deliveries,
        )
