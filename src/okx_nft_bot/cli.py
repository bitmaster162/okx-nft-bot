from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from okx_nft_bot.adapters.binance_support_mapper import BinanceSupportMapper
from okx_nft_bot.analytics.cross_market import detect_spreads, rank_collections
from okx_nft_bot.analytics.reporting import format_rankings_text, format_spreads_text, send_analytics_report
from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.clients.magiceden import MagicEdenClient
from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import load_settings
from okx_nft_bot.fraud.materialize import materialize_from_normalized_events
from okx_nft_bot.fraud.reporting import build_asset_report, build_collection_report, build_wallet_report
from okx_nft_bot.history_backfill import (
    backfill_magiceden_actions_history,
    backfill_okx_actions_history,
    backfill_okx_sales_history,
    backfill_opensea_actions_history,
)
from okx_nft_bot.providers.binance_whitelist import BinanceWhitelistProvider
from okx_nft_bot.providers.offers_okx import OKXOffersProvider
from okx_nft_bot.providers.offers_opensea import OpenSeaOffersProvider
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore
from okx_nft_bot.storage.fraud_store import FraudStore
from okx_nft_bot.deploy_ops import (
    backup_database,
    get_desired_profile,
    list_backups,
    list_profiles,
    resolve_backup_path,
    restore_database,
    set_desired_profile,
)
from okx_nft_bot.mass_offer import MassOfferEngine
from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.notifiers.base import AlertEnvelope
from okx_nft_bot.notifiers.factory import build_notifier
from okx_nft_bot.ops import (
    acknowledge_health_alert,
    get_health_alert_control,
    is_alertable_health_result,
    reset_health_alert_control,
    run_healthcheck,
    snooze_health_alerts,
    write_runtime_metrics,
)
from okx_nft_bot.pipeline.live_cycle import CursorState, Monitor
from okx_nft_bot.pipeline.run_once import run_once
from okx_nft_bot.providers.okx_stub import OKXStubProvider
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramBotClient, TelegramCommandProcessor

logger = logging.getLogger(__name__)


def _build_runner(settings, store):
    notifier = build_notifier(settings)
    registry = CollectionRegistry.from_path(settings.registry_path)
    return MultiCollectionRunner(settings=settings, store=store, notifier=notifier, registry=registry), registry


def _build_offers_store(settings) -> OffersStore:
    return OffersStore(settings.offers_db_path)


def cmd_run_live_cycle(source_mode: str) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    notifier = build_notifier(settings)
    monitor = Monitor(settings=settings, store=store, notifier=notifier)
    result = monitor.run_live_cycle(source_mode=source_mode)
    write_runtime_metrics(settings, store, extra={'daemon_status': 'single_run', 'last_command': 'run-live-cycle'})
    print(json.dumps({
        'active_market': settings.active_market,
        'source_mode': result.source_mode,
        'pages_fetched': result.pages_fetched,
        'start_cursor': result.start_cursor,
        'end_cursor': result.end_cursor,
        'raw_events': len(result.raw_events),
        'new_events': [event.model_dump(mode='json') for event in result.new_events],
        'decisions': [decision.model_dump(mode='json') for decision in result.decisions],
        'deliveries': [delivery.model_dump(mode='json') for delivery in result.deliveries],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run_collection(name: str, source_mode: str) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    runner, _registry = _build_runner(settings, store)
    result = runner.run_collection_once(target_name=name, source_mode=source_mode)
    write_runtime_metrics(settings, store, extra={'daemon_status': 'single_run', 'last_command': 'run-collection'})
    print(json.dumps({
        'target_name': result.target_name,
        'source_mode': result.source_mode,
        'pages_fetched': result.result.pages_fetched,
        'new_events': [event.model_dump(mode='json') for event in result.result.new_events],
        'deliveries': [delivery.model_dump(mode='json') for delivery in result.result.deliveries],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run_all_once() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    runner, _registry = _build_runner(settings, store)
    results = runner.run_all_once()
    write_runtime_metrics(settings, store, extra={'daemon_status': 'single_run', 'last_command': 'run-all-once'})
    print(json.dumps({'runs': [
        {
            'target_name': item.target_name,
            'source_mode': item.source_mode,
            'pages_fetched': item.result.pages_fetched,
            'new_event_count': len(item.result.new_events),
            'delivery_count': sum(1 for delivery in item.result.deliveries if delivery.delivered),
        }
        for item in results
    ]}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run_daemon(max_cycles: int | None) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    runner, _registry = _build_runner(settings, store)
    summary = runner.run_daemon(interval_seconds=settings.scheduler_interval_seconds, max_cycles=max_cycles if max_cycles is not None else settings.daemon_max_cycles)
    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_poll_telegram_once() -> int:
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is required')
    store = SQLiteStore(settings.db_path)
    runner, registry = _build_runner(settings, store)
    transport = StdlibHttpTransport(timeout=settings.okx_request_timeout, max_retries=settings.okx_max_retries, rate_limit_per_sec=settings.okx_rate_limit_per_sec)
    client = TelegramBotClient(bot_token=settings.telegram_bot_token, transport=transport)

    def _load_counter_bidder():
        from pathlib import Path
        import os as _os
        from okx_nft_bot.sniper.counter_bidder import CounterBidder

        wl_path = Path(_os.getenv("BINANCE_WHITELIST_PATH", "./data/binance_whitelist.json"))
        buy_path = Path(_os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
        wl = {}
        if wl_path.exists():
            wl_data = json.loads(wl_path.read_text())
            wl = {item["contract_address"].lower(): item for item in wl_data if item.get("contract_address")}
        buy_cfg = json.loads(buy_path.read_text()) if buy_path.exists() else {}
        return CounterBidder(wl, buy_cfg)

    processor = TelegramCommandProcessor(
        settings=settings,
        store=store,
        registry=registry,
        runner=runner,
        client=client,
        counter_bidder_loader=_load_counter_bidder,
    )
    result = processor.poll_once()
    write_runtime_metrics(settings, store, extra={'daemon_status': 'telegram_poll', 'last_command': 'poll-telegram-once'})
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_list_collections() -> int:
    settings = load_settings()
    registry = CollectionRegistry.from_path(settings.registry_path)
    print(json.dumps([
        {
            'name': item.name,
            'market': item.market,
            'chain': item.chain,
            'collection_address': item.collection_address,
            'collection_slug': item.collection_slug,
            'platform': item.platform,
            'enabled': item.enabled,
            'source_modes': list(item.source_modes),
        }
        for item in registry.collections
    ], ensure_ascii=False, indent=2))
    return 0


def cmd_seed_demo() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    provider = OKXStubProvider()
    result = run_once(provider=provider, store=store, settings=settings)
    write_runtime_metrics(settings, store, extra={'daemon_status': 'seeded_demo', 'last_command': 'seed-demo'})
    print(json.dumps({'events': [event.model_dump(mode='json') for event in result.events], 'decisions': [decision.model_dump(mode='json') for decision in result.decisions]}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_show_events(limit: int) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    print(json.dumps(store.fetch_latest_events(limit=limit), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_fetch_okx_sales_history(
    chain: str,
    start_time: str | None,
    end_time: str | None,
    platform: str | None,
    collection_page_limit: int,
    trade_page_limit: int,
    max_collections: int | None,
    max_trade_pages_per_collection: int | None,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    client = OKXMarketplaceClient(settings=settings)
    result = backfill_okx_sales_history(
        client=client,
        store=store,
        chain=chain,
        platform=platform,
        start_time=start_time,
        end_time=end_time,
        collection_page_limit=collection_page_limit,
        trade_page_limit=trade_page_limit,
        max_collections=max_collections,
        max_trade_pages_per_collection=max_trade_pages_per_collection,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_fetch_okx_actions_history(
    chain: str,
    start_time: str | None,
    end_time: str | None,
    platform: str | None,
    collection_page_limit: int,
    trade_page_limit: int,
    max_collections: int | None,
    max_trade_pages_per_collection: int | None,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    client = OKXMarketplaceClient(settings=settings)
    result = backfill_okx_actions_history(
        client=client,
        store=store,
        chain=chain,
        platform=platform,
        start_time=start_time,
        end_time=end_time,
        collection_page_limit=collection_page_limit,
        trade_page_limit=trade_page_limit,
        max_collections=max_collections,
        max_trade_pages_per_collection=max_trade_pages_per_collection,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_fetch_opensea_actions_history(
    slug: str,
    event_types: list[str],
    limit: int,
    max_pages_per_type: int | None,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    client = OpenSeaClient(settings=settings)
    result = backfill_opensea_actions_history(
        client=client,
        store=store,
        slug=slug,
        event_types=event_types,
        limit=limit,
        max_pages_per_type=max_pages_per_type,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_fetch_magiceden_actions_history(
    chain: str,
    collection: str,
    types: list[str],
    limit: int,
    max_pages: int | None,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    client = MagicEdenClient(settings=settings)
    result = backfill_magiceden_actions_history(
        client=client,
        store=store,
        chain=chain,
        collection=collection,
        types=types,
        limit=limit,
        max_pages=max_pages,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_sync_fraud_canon(market: str | None, limit: int | None) -> int:
    settings = load_settings()
    event_store = SQLiteStore(settings.db_path)
    fraud_store = FraudStore(settings.db_path)
    result = materialize_from_normalized_events(
        event_store=event_store,
        fraud_store=fraud_store,
        market=None if market == 'all' else market,
        limit=limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_analyze_collection(identifier: str, sync_market: str | None, sync_limit: int | None) -> int:
    settings = load_settings()
    event_store = SQLiteStore(settings.db_path)
    fraud_store = FraudStore(settings.db_path)
    materialize_from_normalized_events(
        event_store=event_store,
        fraud_store=fraud_store,
        market=None if sync_market == 'all' else sync_market,
        limit=sync_limit,
    )
    report = build_collection_report(fraud_store, identifier)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_analyze_asset(
    asset_id: str | None,
    collection_identifier: str | None,
    token_id: str | None,
    sync_market: str | None,
    sync_limit: int | None,
) -> int:
    if not asset_id and not (collection_identifier and token_id):
        raise SystemExit('analyze-asset requires --asset-id or --collection plus --token-id')
    settings = load_settings()
    event_store = SQLiteStore(settings.db_path)
    fraud_store = FraudStore(settings.db_path)
    materialize_from_normalized_events(
        event_store=event_store,
        fraud_store=fraud_store,
        market=None if sync_market == 'all' else sync_market,
        limit=sync_limit,
    )
    report = build_asset_report(
        fraud_store,
        asset_id=asset_id,
        collection_identifier=collection_identifier,
        token_id=token_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_analyze_wallet(identifier: str, sync_market: str | None, sync_limit: int | None) -> int:
    settings = load_settings()
    event_store = SQLiteStore(settings.db_path)
    fraud_store = FraudStore(settings.db_path)
    materialize_from_normalized_events(
        event_store=event_store,
        fraud_store=fraud_store,
        market=None if sync_market == 'all' else sync_market,
        limit=sync_limit,
    )
    report = build_wallet_report(fraud_store, identifier)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_watchlist_add(
    object_type: str,
    identifier: str,
    token_id: str | None,
    reason: str,
    priority: str,
    sync_market: str | None,
    sync_limit: int | None,
) -> int:
    settings = load_settings()
    event_store = SQLiteStore(settings.db_path)
    fraud_store = FraudStore(settings.db_path)
    materialize_from_normalized_events(
        event_store=event_store,
        fraud_store=fraud_store,
        market=None if sync_market == 'all' else sync_market,
        limit=sync_limit,
    )

    if object_type == 'collection':
        row = fraud_store.resolve_collection(identifier)
    elif object_type == 'wallet':
        row = fraud_store.resolve_entity(identifier)
    else:
        row = fraud_store.resolve_asset(
            asset_id=identifier if token_id is None else None,
            collection_identifier=identifier if token_id is not None else None,
            token_id=token_id,
        )

    if not row:
        raise SystemExit(f'Unable to resolve {object_type}: {identifier}')

    item = fraud_store.add_watchlist_item(
        object_type=object_type,
        object_id=row['id'],
        reason=reason,
        priority=priority,
    )
    item['object'] = fraud_store.describe_object(object_type=object_type, object_id=row['id'])
    print(json.dumps(item, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_watchlist_show(status: str | None) -> int:
    settings = load_settings()
    fraud_store = FraudStore(settings.db_path)
    items = fraud_store.list_watchlist(status=None if status == 'all' else status)
    for item in items:
        item['object'] = fraud_store.describe_object(object_type=item['object_type'], object_id=item['object_id'])
        if item.get('risk_severity') is not None:
            item['risk_summary'] = {
                'total_score': item.get('total_score'),
                'severity': item.get('risk_severity'),
                'confidence': item.get('risk_confidence'),
                'scored_at': item.get('scored_at'),
            }
    print(json.dumps(items, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_show_offers(
    market: str | None,
    collection: str | None,
    chain: str | None,
    maker: str | None,
    status: str | None,
    min_price: float | None,
    max_price: float | None,
    active_only: bool,
    limit: int,
) -> int:
    settings = load_settings()
    offers_store = _build_offers_store(settings)
    filters = OfferFilters(
        market=market,
        collection=collection,
        chain=chain,
        maker=maker,
        status=status,
        min_price=min_price,
        max_price=max_price,
        active_only=active_only,
        limit=limit,
    )
    offers = offers_store.query_offers(filters)
    print(json.dumps(
        [o.model_dump(mode='json') for o in offers],
        ensure_ascii=False, indent=2, default=str,
    ))
    return 0


def cmd_fetch_offers(
    market: str,
    chain: str | None,
    maker: str | None,
    collection_address: str | None,
    slug: str | None,
    collection: str | None,
    max_pages: int,
) -> int:
    settings = load_settings()
    resolved_chain = chain
    resolved_collection_address = collection_address
    resolved_slug = slug

    if collection:
        if market == 'okx':
            if resolved_collection_address:
                raise SystemExit('fetch-offers: use either --collection-address or legacy --collection, not both')
            resolved_collection_address = collection
        else:
            if resolved_slug:
                raise SystemExit('fetch-offers: use either --slug or legacy --collection, not both')
            resolved_slug = collection

    if market == 'opensea':
        if maker:
            raise SystemExit('fetch-offers: --maker is only valid for market okx')
        if resolved_collection_address:
            raise SystemExit('fetch-offers: --collection-address is only valid for market okx')
        if not resolved_slug:
            raise SystemExit('fetch-offers: --slug is required for market opensea')
        client = OpenSeaClient(settings)
        provider = OpenSeaOffersProvider(client=client, settings=settings)
        offers = provider.fetch_all_pages(
            slug=resolved_slug,
            chain=resolved_chain,
            max_pages=max_pages,
        )
    else:
        if resolved_slug:
            raise SystemExit('fetch-offers: --slug is only valid for market opensea')
        if not resolved_chain:
            raise SystemExit('fetch-offers: --chain is required for market okx')
        if not resolved_collection_address:
            raise SystemExit('fetch-offers: --collection-address is required for market okx')
        client = OKXMarketplaceClient(settings)
        provider = OKXOffersProvider(client=client, settings=settings)
        offers = provider.fetch_all_pages(
            chain=resolved_chain,
            collection_address=resolved_collection_address,
            maker=maker,
            max_pages=max_pages,
        )
    offers_store = _build_offers_store(settings)
    stored = offers_store.upsert_offers(offers)
    print(json.dumps({
        'market': market,
        'fetched': len(offers),
        'stored': stored,
        'chain': resolved_chain,
        'maker': maker,
        'collection': resolved_collection_address if market == 'okx' else resolved_slug,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_mass_offer(
    *,
    collection: str,
    chain: str,
    price: float | None,
    rarity: str | None,
    unlisted_only: bool,
    include_own: bool,
    max_existing_offer: float | None,
    min_token_id: int | None,
    max_token_id: int | None,
    max_offers: int | None,
    duration_hours: int | None,
    delay_seconds: float | None,
    dry_run: bool | None,
) -> int:
    settings = load_settings()
    engine = MassOfferEngine(settings=settings)
    rarity_filter = [part.strip() for part in (rarity or "").split(",") if part.strip()]
    result = engine.run(
        collection=collection,
        chain=chain,
        price_bnb=price,
        rarity_filter=rarity_filter,
        unlisted_only=unlisted_only,
        exclude_own=not include_own,
        max_existing_offer=max_existing_offer,
        min_token_id=min_token_id,
        max_token_id=max_token_id,
        max_total=max_offers,
        duration_hours=duration_hours,
        delay_seconds=delay_seconds,
        dry_run=dry_run,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_status(*, chain: str) -> int:
    settings = load_settings()
    engine = MassOfferEngine(settings=settings)
    payload = engine.status(chain=chain)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_cancel(*, chain: str, collection: str | None) -> int:
    settings = load_settings()
    engine = MassOfferEngine(settings=settings)
    payload = engine.cancel_active(chain=chain, collection=collection)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_compare_markets() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    print(json.dumps(store.fetch_market_summary(), ensure_ascii=False, indent=2, default=str))
    return 0



def _load_binance_mapper() -> BinanceSupportMapper:
    """Load Binance whitelist safely; returns empty mapper on any failure."""
    try:
        provider = BinanceWhitelistProvider()
        entries = provider.load_static()
        return BinanceSupportMapper(entries)
    except Exception as exc:
        logger.warning("Binance whitelist load failed: %s", exc)
        return BinanceSupportMapper([])


def cmd_detect_spreads(min_pct: float, limit: int, sample_limit: int) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_analysis_events(limit=sample_limit)
    mapper = _load_binance_mapper()
    spreads = detect_spreads(rows, min_spread_pct=min_pct, top_n=limit, binance_mapper=mapper)
    print(json.dumps([
        {
            'collection_key': item.collection_key,
            'collection_name': item.collection_name,
            'buy_market': item.buy_market,
            'sell_market': item.sell_market,
            'buy_price': item.buy_price,
            'sell_price': item.sell_price,
            'spread_abs': item.spread_abs,
            'spread_pct': item.spread_pct,
            'observed_at': item.observed_at.isoformat() if item.observed_at else None,
            'markets_seen': item.markets_seen,
            'reference_currency': item.reference_currency,
            'binance_supported': item.binance_supported,
            'binance_list_type': item.binance_list_type,
            'support_confidence': item.support_confidence,
            'support_source_url': item.support_source_url,
            'freshness': item.freshness,
            'confidence': item.confidence,
        }
        for item in spreads
    ], ensure_ascii=False, indent=2))
    return 0


def cmd_rank_collections(min_pct: float, limit: int, sample_limit: int) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_analysis_events(limit=sample_limit)
    mapper = _load_binance_mapper()
    rankings = rank_collections(rows, min_spread_pct=min_pct, top_n=limit, binance_mapper=mapper)
    print(json.dumps([
        {
            'collection_key': item.collection_key,
            'collection_name': item.collection_name,
            'score': item.score,
            'market_count': item.market_count,
            'event_count': item.event_count,
            'listing_count': item.listing_count,
            'sale_count': item.sale_count,
            'volume_24h_total': item.volume_24h_total,
            'best_spread_pct': item.best_spread_pct,
            'latest_event_time': item.latest_event_time.isoformat() if item.latest_event_time else None,
            'signals': list(item.signals),
            'binance_supported': item.binance_supported,
            'binance_list_type': item.binance_list_type,
            'support_confidence': item.support_confidence,
            'support_source_url': item.support_source_url,
            'freshness': item.freshness,
            'confidence': item.confidence,
        }
        for item in rankings
    ], ensure_ascii=False, indent=2))
    return 0


def cmd_send_analytics_alerts(min_pct: float, limit: int, sample_limit: int) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_analysis_events(limit=sample_limit)
    spreads = detect_spreads(rows, min_spread_pct=min_pct, top_n=limit)
    rankings = rank_collections(rows, min_spread_pct=min_pct, top_n=limit)
    payload = send_analytics_report(settings, spreads=spreads, rankings=rankings)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_snapshot_analytics(min_pct: float, limit: int, sample_limit: int) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rows = store.fetch_analysis_events(limit=sample_limit)
    spreads = detect_spreads(rows, min_spread_pct=min_pct, top_n=limit)
    rankings = rank_collections(rows, min_spread_pct=min_pct, top_n=limit)
    print(json.dumps({
        'spread_text': format_spreads_text(spreads),
        'ranking_text': format_rankings_text(rankings),
        'spread_count': len(spreads),
        'ranking_count': len(rankings),
    }, ensure_ascii=False, indent=2))
    return 0

def cmd_show_state() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    print(json.dumps(store.fetch_state_rows(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_reset_cursor(source_mode: str) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    suffix = settings.opensea_cursor_namespace if settings.active_market == 'opensea' else settings.okx_cursor_namespace
    state = CursorState(namespace=f'{settings.active_market}:{source_mode}:{suffix}')
    state.reset(store)
    write_runtime_metrics(settings, store, extra={'daemon_status': 'cursor_reset', 'last_command': 'reset-cursor'})
    print(json.dumps({'reset': True, 'namespace': state.namespace}, ensure_ascii=False, indent=2))
    return 0


def cmd_send_test_alert() -> int:
    settings = load_settings()
    notifier = build_notifier(settings)
    alert = AlertEnvelope(event=NFTEvent(event_id='test:event:1', market='okx', event_type='sale', collection='Test Collection', token_id='1', contract_address='0xabc', price=1.23, currency='ETH', quantity=1, maker='0xseller', taker='0xbuyer', tx_hash='0xtest', event_time=datetime.now(timezone.utc), volume_24h=42.0, floor_price=1.0, raw_source='test'), decision=FilterDecision(event_id='test:event:1', passed=True, matched_rules=['manual_test']))
    result = notifier.send(alert)
    print(json.dumps(result.model_dump(mode='json'), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_write_metrics() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    payload = write_runtime_metrics(settings, store, extra={'daemon_status': 'manual_snapshot', 'last_command': 'write-metrics'})
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_healthcheck(skip_fresh_metrics: bool) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    result = run_healthcheck(settings, store, require_fresh_metrics=not skip_fresh_metrics)
    print(json.dumps({'healthy': result.healthy, 'reason': result.reason, 'age_seconds': result.age_seconds, **result.payload}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.healthy else 1


def cmd_alert_status() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    result = run_healthcheck(settings, store)
    control = get_health_alert_control(store)
    print(
        json.dumps(
            {
                'healthy': result.healthy,
                'reason': result.reason,
                'age_seconds': result.age_seconds,
                'control': control.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_alert_ack(note: str | None) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    result = run_healthcheck(settings, store)
    if not is_alertable_health_result(result):
        raise SystemExit(f'No current alertable health issue to acknowledge (healthy={result.healthy} reason={result.reason})')
    resolved_note = note or 'cli_alert_ack'
    control = acknowledge_health_alert(store, reason=result.reason, actor='cli', note=resolved_note)
    print(
        json.dumps(
            {
                'acknowledged': True,
                'health_reason': result.reason,
                'note': resolved_note,
                'control': control.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_alert_snooze(minutes: int, reason: str | None) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    resolved_reason = reason or 'cli_alert_snooze'
    control = snooze_health_alerts(store, minutes=max(minutes, 1), actor='cli', reason=resolved_reason)
    print(
        json.dumps(
            {
                'snoozed': True,
                'minutes': max(minutes, 1),
                'reason': resolved_reason,
                'control': control.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_alert_reset() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    control = reset_health_alert_control(store)
    print(json.dumps({'reset': True, 'control': control.to_dict()}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_profiles() -> int:
    settings = load_settings()
    print(json.dumps({'runtime_profile': settings.app_profile, 'available_profiles': list(list_profiles(settings.profiles_dir))}, ensure_ascii=False, indent=2))
    return 0


def cmd_show_profile() -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    print(json.dumps({'runtime_profile': settings.app_profile, 'desired_profile': get_desired_profile(store, settings.app_profile), 'profiles_dir': str(settings.profiles_dir)}, ensure_ascii=False, indent=2))
    return 0


def cmd_set_profile(name: str) -> int:
    settings = load_settings()
    available = set(list_profiles(settings.profiles_dir))
    if name not in available:
        raise SystemExit(f'Unknown profile: {name}. Available: {", ".join(sorted(available))}')
    store = SQLiteStore(settings.db_path)
    set_desired_profile(store, name)
    print(json.dumps({'desired_profile': name, 'restart_required': True}, ensure_ascii=False, indent=2))
    return 0


def cmd_backup_db(label: str) -> int:
    settings = load_settings()
    SQLiteStore(settings.db_path)
    artifact = backup_database(settings.db_path, settings.backup_dir, label=label)
    print(json.dumps({'backup_file': artifact.path.name, 'size_bytes': artifact.size_bytes, 'backup_dir': str(settings.backup_dir)}, ensure_ascii=False, indent=2))
    return 0


def cmd_list_backups(limit: int) -> int:
    settings = load_settings()
    items = list_backups(settings.backup_dir, limit=limit)
    print(json.dumps([{'name': item.name, 'size_bytes': item.stat().st_size} for item in items], ensure_ascii=False, indent=2))
    return 0


def cmd_restore_db(name: str, force: bool) -> int:
    if not force:
        raise SystemExit('restore-db requires --yes')
    settings = load_settings()
    SQLiteStore(settings.db_path)
    backup_path = resolve_backup_path(settings.backup_dir, name)
    result = restore_database(settings.db_path, backup_path, settings.backup_dir, create_safety_backup=True)
    print(json.dumps({'restored_from': result.restored_from.name, 'restored_to': str(result.restored_to), 'safety_backup': result.safety_backup_path.name if result.safety_backup_path else None}, ensure_ascii=False, indent=2))
    return 0


def cmd_sales_stream(*, once: bool, interval: int | None, markets: str | None, max_cycles: int | None) -> int:
    settings = load_settings()
    # CLI overrides take precedence over .env
    if interval:
        settings.sales_poll_interval = interval
    if markets:
        settings.sales_markets = markets
    if max_cycles:
        settings.sales_daemon_max_cycles = max_cycles

    from okx_nft_bot.sales_stream import SalesStreamDaemon
    daemon = SalesStreamDaemon.from_settings(settings)
    if once:
        results = daemon.run_once()
        print(json.dumps(results, indent=2))
        return 0
    daemon.run_daemon(max_cycles=settings.sales_daemon_max_cycles or 0)
    return 0


def cmd_sales_stats() -> int:
    settings = load_settings()
    from okx_nft_bot.sales_stream import SalesDatabase
    db = SalesDatabase(str(settings.sales_db_path))
    print(json.dumps(db.get_stats(), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='okx-nft-bot')
    subparsers = parser.add_subparsers(dest='command', required=True)

    run_trades = subparsers.add_parser('run-live-cycle')
    run_trades.add_argument('--source', choices=['trades', 'listings'], default='trades')

    run_collection = subparsers.add_parser('run-collection')
    run_collection.add_argument('name')
    run_collection.add_argument('--source', choices=['trades', 'listings'], default='trades')

    subparsers.add_parser('run-all-once')
    run_daemon = subparsers.add_parser('run-daemon')
    run_daemon.add_argument('--max-cycles', type=int)
    subparsers.add_parser('poll-telegram-once')
    subparsers.add_parser('list-collections')
    subparsers.add_parser('seed-demo')
    show_events = subparsers.add_parser('show-events')
    show_events.add_argument('--limit', type=int, default=20)
    fetch_okx_sales_history = subparsers.add_parser('fetch-okx-sales-history')
    fetch_okx_sales_history.add_argument('--chain', default='eth')
    fetch_okx_sales_history.add_argument('--start-time', default=None, help='Unix timestamp in seconds or milliseconds')
    fetch_okx_sales_history.add_argument('--end-time', default=None, help='Unix timestamp in seconds or milliseconds')
    fetch_okx_sales_history.add_argument('--platform', default=None, help='Optional platform filter, e.g. OKX, OpenSea, Blur')
    fetch_okx_sales_history.add_argument('--collection-page-limit', type=int, default=300)
    fetch_okx_sales_history.add_argument('--trade-page-limit', type=int, default=50)
    fetch_okx_sales_history.add_argument('--max-collections', type=int, default=None)
    fetch_okx_sales_history.add_argument('--max-trade-pages-per-collection', type=int, default=None)
    fetch_okx_actions_history = subparsers.add_parser('fetch-okx-actions-history')
    fetch_okx_actions_history.add_argument('--chain', default='eth')
    fetch_okx_actions_history.add_argument('--start-time', default=None, help='Unix timestamp in seconds or milliseconds')
    fetch_okx_actions_history.add_argument('--end-time', default=None, help='Unix timestamp in seconds or milliseconds')
    fetch_okx_actions_history.add_argument('--platform', default=None, help='Optional platform filter, e.g. OKX, OpenSea, Blur')
    fetch_okx_actions_history.add_argument('--collection-page-limit', type=int, default=300)
    fetch_okx_actions_history.add_argument('--trade-page-limit', type=int, default=50)
    fetch_okx_actions_history.add_argument('--max-collections', type=int, default=None)
    fetch_okx_actions_history.add_argument('--max-trade-pages-per-collection', type=int, default=None)
    fetch_opensea_actions_history = subparsers.add_parser('fetch-opensea-actions-history')
    fetch_opensea_actions_history.add_argument('--slug', required=True)
    fetch_opensea_actions_history.add_argument('--event-types', nargs='+', default=['sale', 'listing', 'offer', 'transfer'])
    fetch_opensea_actions_history.add_argument('--limit', type=int, default=50)
    fetch_opensea_actions_history.add_argument('--max-pages-per-type', type=int, default=None)
    fetch_magiceden_actions_history = subparsers.add_parser('fetch-magiceden-actions-history')
    fetch_magiceden_actions_history.add_argument('--chain', default='ethereum')
    fetch_magiceden_actions_history.add_argument('--collection', required=True)
    fetch_magiceden_actions_history.add_argument('--types', nargs='+', default=['sale', 'ask', 'bid', 'transfer'])
    fetch_magiceden_actions_history.add_argument('--limit', type=int, default=50)
    fetch_magiceden_actions_history.add_argument('--max-pages', type=int, default=None)
    sync_fraud = subparsers.add_parser('sync-fraud-canon')
    sync_fraud.add_argument('--market', choices=['okx', 'opensea', 'magiceden', 'all'], default='okx')
    sync_fraud.add_argument('--limit', type=int, default=None)
    analyze_collection = subparsers.add_parser('analyze-collection')
    analyze_collection.add_argument('identifier')
    analyze_collection.add_argument('--market', choices=['okx', 'opensea', 'magiceden', 'all'], default='okx')
    analyze_collection.add_argument('--limit', type=int, default=None)
    analyze_asset = subparsers.add_parser('analyze-asset')
    analyze_asset.add_argument('--asset-id', default=None)
    analyze_asset.add_argument('--collection', default=None)
    analyze_asset.add_argument('--token-id', default=None)
    analyze_asset.add_argument('--market', choices=['okx', 'opensea', 'magiceden', 'all'], default='okx')
    analyze_asset.add_argument('--limit', type=int, default=None)
    analyze_wallet = subparsers.add_parser('analyze-wallet')
    analyze_wallet.add_argument('identifier')
    analyze_wallet.add_argument('--market', choices=['okx', 'opensea', 'magiceden', 'all'], default='okx')
    analyze_wallet.add_argument('--limit', type=int, default=None)
    watchlist_add = subparsers.add_parser('watchlist-add')
    watchlist_add.add_argument('--type', choices=['collection', 'asset', 'wallet'], required=True)
    watchlist_add.add_argument('--identifier', required=True)
    watchlist_add.add_argument('--token-id', default=None)
    watchlist_add.add_argument('--reason', required=True)
    watchlist_add.add_argument('--priority', choices=['low', 'medium', 'high', 'critical'], default='medium')
    watchlist_add.add_argument('--market', choices=['okx', 'opensea', 'magiceden', 'all'], default='okx')
    watchlist_add.add_argument('--limit', type=int, default=None)
    watchlist_show = subparsers.add_parser('watchlist-show')
    watchlist_show.add_argument('--status', choices=['active', 'inactive', 'all'], default='active')
    fetch_offers_p = subparsers.add_parser('fetch-offers')
    fetch_offers_p.add_argument('--market', choices=['okx', 'opensea'], default='okx')
    fetch_offers_p.add_argument('--chain', default=None)
    fetch_offers_p.add_argument('--maker', default=None)
    fetch_offers_p.add_argument('--collection-address', default=None)
    fetch_offers_p.add_argument('--slug', default=None)
    fetch_offers_p.add_argument('--collection', default=None, help=argparse.SUPPRESS)
    fetch_offers_p.add_argument('--max-pages', type=int, default=5)
    show_offers_p = subparsers.add_parser('show-offers')
    show_offers_p.add_argument('--market', choices=['okx', 'opensea', 'all'], default=None)
    show_offers_p.add_argument('--collection', default=None)
    show_offers_p.add_argument('--chain', default=None)
    show_offers_p.add_argument('--maker', default=None)
    show_offers_p.add_argument('--status', choices=['active', 'inactive', 'cancelled', 'sold', 'all'], default=None)
    show_offers_p.add_argument('--min-price', type=float, default=None)
    show_offers_p.add_argument('--max-price', type=float, default=None)
    show_offers_p.add_argument('--active-only', action='store_true')
    show_offers_p.add_argument('--limit', type=int, default=50)
    mass_offer_p = subparsers.add_parser('mass-offer')
    mass_offer_p.add_argument('--collection', required=True)
    mass_offer_p.add_argument('--chain', default='bsc')
    mass_offer_p.add_argument('--price', type=float, default=None)
    mass_offer_p.add_argument('--rarity', default=None)
    mass_offer_p.add_argument('--unlisted-only', action='store_true')
    mass_offer_p.add_argument('--include-own', action='store_true')
    mass_offer_p.add_argument('--max-existing-offer', type=float, default=None)
    mass_offer_p.add_argument('--min-token-id', type=int, default=None)
    mass_offer_p.add_argument('--max-token-id', type=int, default=None)
    mass_offer_p.add_argument('--max-offers', type=int, default=None)
    mass_offer_p.add_argument('--duration-hours', type=int, default=None)
    mass_offer_p.add_argument('--delay-seconds', type=float, default=None)
    mass_offer_p.add_argument('--dry-run', dest='dry_run', action='store_const', const=True, default=None)
    mass_offer_status_p = subparsers.add_parser('mass-offer-status')
    mass_offer_status_p.add_argument('--chain', default='bsc')
    mass_offer_cancel_p = subparsers.add_parser('mass-offer-cancel')
    mass_offer_cancel_p.add_argument('--chain', default='bsc')
    mass_offer_cancel_p.add_argument('--collection', default=None)
    subparsers.add_parser('compare-markets')
    detect_spreads_p = subparsers.add_parser('detect-spreads')
    detect_spreads_p.add_argument('--min-pct', type=float, default=3.0)
    detect_spreads_p.add_argument('--limit', type=int, default=10)
    detect_spreads_p.add_argument('--sample-limit', type=int, default=5000)
    rank_collections_p = subparsers.add_parser('rank-collections')
    rank_collections_p.add_argument('--min-pct', type=float, default=3.0)
    rank_collections_p.add_argument('--limit', type=int, default=10)
    rank_collections_p.add_argument('--sample-limit', type=int, default=5000)
    snapshot_analytics_p = subparsers.add_parser('snapshot-analytics')
    snapshot_analytics_p.add_argument('--min-pct', type=float, default=3.0)
    snapshot_analytics_p.add_argument('--limit', type=int, default=10)
    snapshot_analytics_p.add_argument('--sample-limit', type=int, default=5000)
    send_analytics_p = subparsers.add_parser('send-analytics-alerts')
    send_analytics_p.add_argument('--min-pct', type=float, default=3.0)
    send_analytics_p.add_argument('--limit', type=int, default=5)
    send_analytics_p.add_argument('--sample-limit', type=int, default=5000)
    subparsers.add_parser('show-state')
    reset_cursor = subparsers.add_parser('reset-cursor')
    reset_cursor.add_argument('--source', choices=['trades', 'listings'], default='trades')
    subparsers.add_parser('send-test-alert')
    subparsers.add_parser('write-metrics')
    healthcheck = subparsers.add_parser('healthcheck')
    healthcheck.add_argument('--skip-fresh-metrics', action='store_true')
    subparsers.add_parser('alert-status')
    alert_ack = subparsers.add_parser('alert-ack')
    alert_ack.add_argument('note', nargs='*')
    alert_snooze = subparsers.add_parser('alert-snooze')
    alert_snooze.add_argument('--minutes', type=int, default=60)
    alert_snooze.add_argument('reason', nargs='*')
    subparsers.add_parser('alert-reset')
    subparsers.add_parser('profiles')
    subparsers.add_parser('show-profile')
    set_profile = subparsers.add_parser('set-profile')
    set_profile.add_argument('name', choices=['dev', 'stage', 'prod'])
    backup_db = subparsers.add_parser('backup-db')
    backup_db.add_argument('--label', default='manual')
    list_backups_p = subparsers.add_parser('list-backups')
    list_backups_p.add_argument('--limit', type=int, default=10)
    restore_db = subparsers.add_parser('restore-db')
    restore_db.add_argument('name')
    restore_db.add_argument('--yes', action='store_true')

    # ── Sales Stream ──
    sales_stream = subparsers.add_parser('sales-stream', help='Run real-time sales stream daemon (OKX, OpenSea, MagicEden)')
    sales_stream.add_argument('--once', action='store_true', help='Single poll pass then exit')
    sales_stream.add_argument('--interval', type=int, default=None, help='Poll interval in seconds (default: 30)')
    sales_stream.add_argument('--markets', type=str, default=None, help='Comma-separated markets: okx,opensea,magiceden')
    sales_stream.add_argument('--max-cycles', type=int, default=None, help='Stop after N cycles (default: forever)')
    subparsers.add_parser('sales-stats', help='Show sales stream DB statistics')
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'run-live-cycle':
        return cmd_run_live_cycle(source_mode=args.source)
    if args.command == 'run-collection':
        return cmd_run_collection(name=args.name, source_mode=args.source)
    if args.command == 'run-all-once':
        return cmd_run_all_once()
    if args.command == 'run-daemon':
        return cmd_run_daemon(max_cycles=args.max_cycles)
    if args.command == 'poll-telegram-once':
        return cmd_poll_telegram_once()
    if args.command == 'list-collections':
        return cmd_list_collections()
    if args.command == 'seed-demo':
        return cmd_seed_demo()
    if args.command == 'show-events':
        return cmd_show_events(limit=args.limit)
    if args.command == 'fetch-okx-sales-history':
        return cmd_fetch_okx_sales_history(
            chain=args.chain,
            start_time=args.start_time,
            end_time=args.end_time,
            platform=args.platform,
            collection_page_limit=args.collection_page_limit,
            trade_page_limit=args.trade_page_limit,
            max_collections=args.max_collections,
            max_trade_pages_per_collection=args.max_trade_pages_per_collection,
        )
    if args.command == 'fetch-okx-actions-history':
        return cmd_fetch_okx_actions_history(
            chain=args.chain,
            start_time=args.start_time,
            end_time=args.end_time,
            platform=args.platform,
            collection_page_limit=args.collection_page_limit,
            trade_page_limit=args.trade_page_limit,
            max_collections=args.max_collections,
            max_trade_pages_per_collection=args.max_trade_pages_per_collection,
        )
    if args.command == 'fetch-opensea-actions-history':
        return cmd_fetch_opensea_actions_history(
            slug=args.slug,
            event_types=args.event_types,
            limit=args.limit,
            max_pages_per_type=args.max_pages_per_type,
        )
    if args.command == 'fetch-magiceden-actions-history':
        return cmd_fetch_magiceden_actions_history(
            chain=args.chain,
            collection=args.collection,
            types=args.types,
            limit=args.limit,
            max_pages=args.max_pages,
        )
    if args.command == 'sync-fraud-canon':
        return cmd_sync_fraud_canon(market=args.market, limit=args.limit)
    if args.command == 'analyze-collection':
        return cmd_analyze_collection(identifier=args.identifier, sync_market=args.market, sync_limit=args.limit)
    if args.command == 'analyze-asset':
        return cmd_analyze_asset(
            asset_id=args.asset_id,
            collection_identifier=args.collection,
            token_id=args.token_id,
            sync_market=args.market,
            sync_limit=args.limit,
        )
    if args.command == 'analyze-wallet':
        return cmd_analyze_wallet(identifier=args.identifier, sync_market=args.market, sync_limit=args.limit)
    if args.command == 'watchlist-add':
        return cmd_watchlist_add(
            object_type=args.type,
            identifier=args.identifier,
            token_id=args.token_id,
            reason=args.reason,
            priority=args.priority,
            sync_market=args.market,
            sync_limit=args.limit,
        )
    if args.command == 'watchlist-show':
        return cmd_watchlist_show(status=args.status)
    if args.command == 'fetch-offers':
        return cmd_fetch_offers(
            market=args.market,
            chain=args.chain,
            maker=args.maker,
            collection_address=args.collection_address,
            slug=args.slug,
            collection=args.collection,
            max_pages=args.max_pages,
        )
    if args.command == 'show-offers':
        return cmd_show_offers(
            market=args.market if args.market != 'all' else None,
            collection=args.collection,
            chain=args.chain,
            maker=args.maker,
            status=args.status if args.status != 'all' else None,
            min_price=args.min_price,
            max_price=args.max_price,
            active_only=args.active_only,
            limit=args.limit,
        )
    if args.command == 'mass-offer':
        return cmd_mass_offer(
            collection=args.collection,
            chain=args.chain,
            price=args.price,
            rarity=args.rarity,
            unlisted_only=args.unlisted_only,
            include_own=args.include_own,
            max_existing_offer=args.max_existing_offer,
            min_token_id=args.min_token_id,
            max_token_id=args.max_token_id,
            max_offers=args.max_offers,
            duration_hours=args.duration_hours,
            delay_seconds=args.delay_seconds,
            dry_run=args.dry_run,
        )
    if args.command == 'mass-offer-status':
        return cmd_mass_offer_status(chain=args.chain)
    if args.command == 'mass-offer-cancel':
        return cmd_mass_offer_cancel(chain=args.chain, collection=args.collection)
    if args.command == 'compare-markets':
        return cmd_compare_markets()
    if args.command == 'detect-spreads':
        return cmd_detect_spreads(min_pct=args.min_pct, limit=args.limit, sample_limit=args.sample_limit)
    if args.command == 'rank-collections':
        return cmd_rank_collections(min_pct=args.min_pct, limit=args.limit, sample_limit=args.sample_limit)
    if args.command == 'snapshot-analytics':
        return cmd_snapshot_analytics(min_pct=args.min_pct, limit=args.limit, sample_limit=args.sample_limit)
    if args.command == 'send-analytics-alerts':
        return cmd_send_analytics_alerts(min_pct=args.min_pct, limit=args.limit, sample_limit=args.sample_limit)
    if args.command == 'show-state':
        return cmd_show_state()
    if args.command == 'reset-cursor':
        return cmd_reset_cursor(source_mode=args.source)
    if args.command == 'send-test-alert':
        return cmd_send_test_alert()
    if args.command == 'write-metrics':
        return cmd_write_metrics()
    if args.command == 'healthcheck':
        return cmd_healthcheck(skip_fresh_metrics=args.skip_fresh_metrics)
    if args.command == 'alert-status':
        return cmd_alert_status()
    if args.command == 'alert-ack':
        return cmd_alert_ack(note=' '.join(args.note).strip() or None)
    if args.command == 'alert-snooze':
        return cmd_alert_snooze(minutes=args.minutes, reason=' '.join(args.reason).strip() or None)
    if args.command == 'alert-reset':
        return cmd_alert_reset()
    if args.command == 'profiles':
        return cmd_profiles()
    if args.command == 'show-profile':
        return cmd_show_profile()
    if args.command == 'set-profile':
        return cmd_set_profile(name=args.name)
    if args.command == 'backup-db':
        return cmd_backup_db(label=args.label)
    if args.command == 'list-backups':
        return cmd_list_backups(limit=args.limit)
    if args.command == 'restore-db':
        return cmd_restore_db(name=args.name, force=args.yes)
    if args.command == 'sales-stream':
        return cmd_sales_stream(
            once=args.once,
            interval=args.interval,
            markets=args.markets,
            max_cycles=args.max_cycles,
        )
    if args.command == 'sales-stats':
        return cmd_sales_stats()
    parser.error('Unknown command')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
