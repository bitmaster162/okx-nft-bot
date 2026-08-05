from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from okx_nft_bot.adapters.binance_support_mapper import BinanceSupportMapper
from okx_nft_bot.analytics import (
    ExecutionFillReconciler,
    ExecutionHealthAnalyzer,
    PnlGuardAnalyzer,
    PortfolioRiskAnalyzer,
    WalletPnlAnalyzer,
    format_execution_fill_text,
    format_execution_health_text,
    format_pnl_guard_text,
    format_portfolio_risk_text,
    format_wallet_pnl_text,
)
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
from okx_nft_bot.mass_offer import (
    MassOfferAllocator,
    MassOfferBatchRunner,
    MassOfferBudgetScheduler,
    MassOfferBudgetRebalancer,
    MassOfferCircuitBreaker,
    MassOfferQuarantineController,
    MassOfferUnwindController,
    MassOfferEconomics,
    MassOfferEngine,
    MassOfferFeedbackController,
    MassOfferPlanner,
    format_mass_offer_allocator_text,
    format_mass_offer_batch_text,
    format_mass_offer_budget_text,
    format_mass_offer_capital_text,
    format_mass_offer_quarantine_text,
    format_mass_offer_rebalance_text,
    format_mass_offer_circuit_text,
    format_mass_offer_unwind_execution_text,
    format_mass_offer_unwind_text,
    format_mass_offer_economics_text,
    format_mass_offer_feedback_text,
    format_mass_offer_plan_text,
    format_mass_offer_policy_preview,
)
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
from okx_nft_bot.pydantic_compat import model_dump_compat
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
        'new_events': [model_dump_compat(event, mode='json') for event in result.new_events],
        'decisions': [model_dump_compat(decision, mode='json') for decision in result.decisions],
        'deliveries': [model_dump_compat(delivery, mode='json') for delivery in result.deliveries],
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
        'new_events': [model_dump_compat(event, mode='json') for event in result.result.new_events],
        'deliveries': [model_dump_compat(delivery, mode='json') for delivery in result.result.deliveries],
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
    processor = TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=client)
    # Wire ParasiteHunter if available
    try:
        from okx_nft_bot.sniper.parasite_hunter import ParasiteHunter
        from pathlib import Path
        import os as _os
        wl_path = Path(_os.getenv("BINANCE_WHITELIST_PATH", "./data/binance_whitelist.json"))
        buy_path = Path(_os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
        wl = {}
        if wl_path.exists():
            wl_data = json.loads(wl_path.read_text())
            wl = {item["contract_address"].lower(): item for item in wl_data if item.get("contract_address")}
        buy_cfg = json.loads(buy_path.read_text()) if buy_path.exists() else {}
        processor.parasite_hunter = ParasiteHunter(wl, buy_cfg)
    except Exception as exc:
        logger.warning("ParasiteHunter init failed (telegram): %s", exc)
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
    print(json.dumps({'events': [model_dump_compat(event, mode='json') for event in result.events], 'decisions': [model_dump_compat(decision, mode='json') for decision in result.decisions]}, ensure_ascii=False, indent=2, default=str))
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
        [model_dump_compat(o, mode='json') for o in offers],
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


def cmd_mass_offer_policy_preview(
    *,
    collection: str,
    chain: str,
    price: float | None,
    max_offers: int | None,
    delay_seconds: float | None,
    max_existing_offer: float | None,
    dry_run: bool | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    engine = MassOfferEngine(settings=settings)
    payload = engine.preview_policy(
        collection=collection,
        chain=chain,
        price_bnb=price,
        dry_run=dry_run,
        max_total=max_offers,
        delay_seconds=delay_seconds,
        max_existing_offer=max_existing_offer,
    )
    if as_text:
        print(format_mass_offer_policy_preview(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_allocator(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    allocator = MassOfferAllocator(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_allocator_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = allocator.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
    )
    written_paths: dict[str, str] | None = None
    if write:
        written_paths = allocator.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            limit=policy_limit,
        )
    if as_text:
        print(format_mass_offer_allocator_text(report, limit=limit))
        if written_paths:
            print(f"\nwritten_report={written_paths['report_path']}\nwritten_policy={written_paths['policy_path']}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_feedback(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    controller = MassOfferFeedbackController(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_feedback_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = controller.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
    )
    written_paths: dict[str, str] | None = None
    if write:
        written_paths = controller.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            limit=policy_limit,
        )
    if as_text:
        print(format_mass_offer_feedback_text(report, limit=limit))
        if written_paths:
            print(f"\nwritten_report={written_paths['report_path']}\nwritten_policy={written_paths['policy_path']}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_circuit(
    *,
    wallet: str | None,
    chain: str,
    window_hours: int | None,
    limit: int,
    write: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    breaker = MassOfferCircuitBreaker(settings=settings)
    resolved_window_hours = int(window_hours if window_hours is not None else settings.mass_offer_circuit_window_hours)
    report = breaker.build_report(
        wallet=wallet,
        chain=chain,
        window_hours=resolved_window_hours,
    )
    written_path: str | None = None
    if write:
        written_path = breaker.write_report(
            wallet=wallet,
            chain=chain,
            window_hours=resolved_window_hours,
        )
    if as_text:
        print(format_mass_offer_circuit_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload["preview_limit"] = limit
        if written_path:
            payload["written_report"] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_economics(
    *,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    economics = MassOfferEconomics(settings=settings)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_economics_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = economics.build_report(chain=chain, window_days=resolved_window_days, event_limit=resolved_event_limit)
    written_paths: dict[str, str] | None = None
    if write:
        written_paths = economics.write_report(
            chain=chain,
            window_days=resolved_window_days,
            event_limit=resolved_event_limit,
            limit=policy_limit,
        )
    if as_text:
        print(format_mass_offer_economics_text(report, limit=limit))
        if written_paths:
            print(f"\nwritten_report={written_paths['report_path']}\nwritten_policy={written_paths['policy_path']}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_wallet_pnl(
    *,
    wallet: str | None,
    limit: int,
    write: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    analyzer = WalletPnlAnalyzer(settings=settings, store=store)
    report = analyzer.build_report(
        wallet=wallet,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        collection_limit=limit,
        open_limit=limit,
        closed_limit=max(limit, 1) * 2,
    )
    written_path: str | None = None
    if write:
        written_path = analyzer.write_report(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
        )
    if as_text:
        print(format_wallet_pnl_text(report, collection_limit=limit, position_limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_execution_fills(
    *,
    wallet: str | None,
    limit: int,
    write: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    reconciler = ExecutionFillReconciler(settings=settings, store=store)
    report = reconciler.reconcile(
        wallet=wallet,
        reference_limit=settings.execution_fill_reference_event_limit,
        chain=settings.execution_chain,
        window_hours=settings.execution_fill_reconcile_window_hours,
        price_tolerance_pct=settings.execution_fill_price_tolerance_pct,
        pre_submit_slack_minutes=settings.execution_fill_pre_submit_slack_minutes,
        limit=max(limit, 1) * 3,
    )
    written_path: str | None = None
    if write:
        written_path = reconciler.write_report(
            wallet=wallet,
            reference_limit=settings.execution_fill_reference_event_limit,
            chain=settings.execution_chain,
            window_hours=settings.execution_fill_reconcile_window_hours,
            price_tolerance_pct=settings.execution_fill_price_tolerance_pct,
            pre_submit_slack_minutes=settings.execution_fill_pre_submit_slack_minutes,
        )
    if as_text:
        print(format_execution_fill_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0




def cmd_portfolio_risk(
    *,
    wallet: str | None,
    limit: int,
    write: bool,
    apply_guardrails: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    analyzer = PortfolioRiskAnalyzer(settings=settings, store=store)
    report = (
        analyzer.evaluate_and_apply(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
        )
        if apply_guardrails
        else analyzer.build_report(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
        )
    )
    written_path: str | None = None
    if write:
        written_path = analyzer.write_report(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
            apply_guardrails=apply_guardrails,
        )
    if as_text:
        print(format_portfolio_risk_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        payload['apply_guardrails'] = apply_guardrails
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0



def cmd_pnl_guard(
    *,
    wallet: str | None,
    limit: int,
    write: bool,
    apply_guardrails: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    analyzer = PnlGuardAnalyzer(settings=settings, store=store)
    report = (
        analyzer.evaluate_and_apply(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
            window_hours=settings.pnl_guard_window_hours,
        )
        if apply_guardrails
        else analyzer.build_report(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
            window_hours=settings.pnl_guard_window_hours,
        )
    )
    written_path: str | None = None
    if write:
        written_path = analyzer.write_report(
            wallet=wallet,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            chain=settings.execution_chain,
            window_hours=settings.pnl_guard_window_hours,
            apply_guardrails=apply_guardrails,
        )
    if as_text:
        print(format_pnl_guard_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        payload['apply_guardrails'] = apply_guardrails
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_execution_health(
    *,
    limit: int,
    write: bool,
    apply_guardrails: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    analyzer = ExecutionHealthAnalyzer(settings=settings)
    report = (
        analyzer.evaluate_and_apply(
            chain=settings.execution_chain,
            window_hours=settings.execution_health_window_hours,
            event_limit=settings.execution_health_event_limit,
        )
        if apply_guardrails
        else analyzer.build_report(
            chain=settings.execution_chain,
            window_hours=settings.execution_health_window_hours,
            event_limit=settings.execution_health_event_limit,
        )
    )
    written_path: str | None = None
    if write:
        written_path = analyzer.write_report(
            chain=settings.execution_chain,
            window_hours=settings.execution_health_window_hours,
            event_limit=settings.execution_health_event_limit,
            apply_guardrails=apply_guardrails,
        )
    if as_text:
        print(format_execution_health_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        payload['apply_guardrails'] = apply_guardrails
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_budget(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    price: float | None,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    scheduler = MassOfferBudgetScheduler(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_allocator_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = scheduler.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        price_bnb=price,
    )
    written_paths: dict[str, str] | None = None
    if write:
        written_paths = scheduler.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            price_bnb=price,
            limit=policy_limit,
        )
    if as_text:
        print(format_mass_offer_budget_text(report, limit=limit))
        if written_paths:
            print(
                f"\nwritten_report={written_paths['report_path']}\n"
                f"written_policy={written_paths['policy_path']}"
            )
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_quarantine(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    price: float | None,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    quarantine = MassOfferQuarantineController(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_quarantine_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = quarantine.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        price_bnb=price,
    )
    written_paths: dict[str, str] | None = None
    if write:
        written_report = quarantine.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            price_bnb=price,
            limit=policy_limit,
        )
        written_paths = {
            "report_path": written_report,
            "policy_path": str(settings.mass_offer_quarantine_policy_path),
        }
    if as_text:
        print(format_mass_offer_quarantine_text(report, limit=limit))
        if written_paths:
            print(
                f"\nwritten_report={written_paths['report_path']}\n"
                f"written_policy={written_paths['policy_path']}"
            )
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_rebalance(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    price: float | None,
    policy_limit: int | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    rebalancer = MassOfferBudgetRebalancer(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_rebalance_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = rebalancer.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        price_bnb=price,
    )
    written_paths: dict[str, str] | None = None
    if write:
        written_paths = rebalancer.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            price_bnb=price,
            limit=policy_limit,
        )
    if as_text:
        print(format_mass_offer_rebalance_text(report, limit=limit))
        if written_paths:
            print(
                f"\nwritten_report={written_paths['report_path']}\n"
                f"written_policy={written_paths['policy_path']}"
            )
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_paths:
            payload['written_paths'] = written_paths
        if policy_limit is not None:
            payload['policy_overrides_preview'] = report.to_policy_overrides(limit=policy_limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_unwind(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    target_bnb: float | None,
    max_cancels: int | None,
    apply: bool,
    dry_run: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    controller = MassOfferUnwindController(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_unwind_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = controller.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        target_release_bnb=target_bnb,
        max_cancels=max_cancels,
    )
    written_report: str | None = None
    if write:
        written_report = controller.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            target_release_bnb=target_bnb,
            max_cancels=max_cancels,
        )
    execution = controller.execute_report(report, dry_run=dry_run) if apply else None
    if as_text:
        print(format_mass_offer_unwind_text(report, limit=limit))
        if written_report:
            print(f"\nwritten_report={written_report}")
        if execution is not None:
            print(f"\n{format_mass_offer_unwind_execution_text(execution)}")
    else:
        payload: dict[str, object] = report.to_dict()
        payload['preview_limit'] = limit
        if written_report:
            payload['written_report'] = written_report
        if execution is not None:
            payload['execution'] = execution.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_plan(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    limit: int,
    write: bool,
    price: float | None,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    planner = MassOfferPlanner(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_allocator_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    report = planner.build_report(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        price_bnb=price,
    )
    written_path: str | None = None
    if write:
        written_path = planner.write_report(
            wallet=wallet,
            chain=chain,
            window_days=resolved_window_days,
            reference_limit=settings.wallet_pnl_reference_event_limit,
            event_limit=resolved_event_limit,
            price_bnb=price,
        )
    if as_text:
        print(format_mass_offer_plan_text(report, limit=limit))
        if written_path:
            print(f"\nwritten_report={written_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = limit
        if written_path:
            payload['written_report'] = written_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_batch(
    *,
    wallet: str | None,
    chain: str,
    window_days: int | None,
    event_limit: int | None,
    collection_limit: int | None,
    write: bool,
    price: float | None,
    dry_run: bool | None,
    include_dry_run: bool,
    as_text: bool,
) -> int:
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    runner = MassOfferBatchRunner(settings=settings, store=store)
    resolved_window_days = int(window_days if window_days is not None else settings.mass_offer_allocator_window_days)
    resolved_event_limit = int(event_limit if event_limit is not None else settings.mass_offer_economics_event_limit)
    resolved_collection_limit = int(collection_limit if collection_limit is not None else settings.mass_offer_batch_collection_limit)
    report = runner.run_batch(
        wallet=wallet,
        chain=chain,
        window_days=resolved_window_days,
        reference_limit=settings.wallet_pnl_reference_event_limit,
        event_limit=resolved_event_limit,
        price_bnb=price,
        collection_limit=resolved_collection_limit,
        include_dry_run_collections=include_dry_run if include_dry_run else None,
        dry_run=dry_run,
        write_report=write,
    )
    if as_text:
        print(format_mass_offer_batch_text(report, limit=resolved_collection_limit))
        if write:
            print(f"\nwritten_report={report.report_path}")
    else:
        payload = report.to_dict()
        payload['preview_limit'] = resolved_collection_limit
        if write:
            payload['written_report'] = report.report_path
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_mass_offer_capital(*, chain: str, limit: int, as_text: bool) -> int:
    settings = load_settings()
    engine = MassOfferEngine(settings=settings)
    payload = engine.capital_status(chain=chain, limit=limit)
    if as_text:
        print(format_mass_offer_capital_text(payload))
    else:
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
    print(json.dumps(model_dump_compat(result, mode='json'), ensure_ascii=False, indent=2, default=str))
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
    mass_offer_policy_preview_p = subparsers.add_parser('mass-offer-policy-preview')
    mass_offer_policy_preview_p.add_argument('--collection', required=True)
    mass_offer_policy_preview_p.add_argument('--chain', default='bsc')
    mass_offer_policy_preview_p.add_argument('--price', type=float, default=None)
    mass_offer_policy_preview_p.add_argument('--max-offers', type=int, default=None)
    mass_offer_policy_preview_p.add_argument('--delay-seconds', type=float, default=None)
    mass_offer_policy_preview_p.add_argument('--max-existing-offer', type=float, default=None)
    mass_offer_policy_preview_p.add_argument('--dry-run', dest='dry_run', action='store_const', const=True, default=None)
    mass_offer_policy_preview_p.add_argument('--live', dest='dry_run', action='store_const', const=False)
    mass_offer_policy_preview_p.add_argument('--text', action='store_true')
    mass_offer_econ_p = subparsers.add_parser('mass-offer-economics')
    mass_offer_econ_p.add_argument('--chain', default='bsc')
    mass_offer_econ_p.add_argument('--window-days', type=int, default=None)
    mass_offer_econ_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_econ_p.add_argument('--limit', type=int, default=5)
    mass_offer_econ_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_econ_p.add_argument('--write', action='store_true')
    mass_offer_econ_p.add_argument('--text', action='store_true')
    mass_offer_alloc_p = subparsers.add_parser('mass-offer-allocator')
    mass_offer_alloc_p.add_argument('--wallet', default=None)
    mass_offer_alloc_p.add_argument('--chain', default='bsc')
    mass_offer_alloc_p.add_argument('--window-days', type=int, default=None)
    mass_offer_alloc_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_alloc_p.add_argument('--limit', type=int, default=5)
    mass_offer_alloc_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_alloc_p.add_argument('--write', action='store_true')
    mass_offer_alloc_p.add_argument('--text', action='store_true')
    mass_offer_feedback_p = subparsers.add_parser('mass-offer-feedback')
    mass_offer_feedback_p.add_argument('--wallet', default=None)
    mass_offer_feedback_p.add_argument('--chain', default='bsc')
    mass_offer_feedback_p.add_argument('--window-days', type=int, default=None)
    mass_offer_feedback_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_feedback_p.add_argument('--limit', type=int, default=5)
    mass_offer_feedback_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_feedback_p.add_argument('--write', action='store_true')
    mass_offer_feedback_p.add_argument('--text', action='store_true')
    mass_offer_budget_p = subparsers.add_parser('mass-offer-budget')
    mass_offer_budget_p.add_argument('--wallet', default=None)
    mass_offer_budget_p.add_argument('--chain', default='bsc')
    mass_offer_budget_p.add_argument('--window-days', type=int, default=None)
    mass_offer_budget_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_budget_p.add_argument('--price', type=float, default=None)
    mass_offer_budget_p.add_argument('--limit', type=int, default=5)
    mass_offer_budget_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_budget_p.add_argument('--write', action='store_true')
    mass_offer_budget_p.add_argument('--text', action='store_true')
    mass_offer_circuit_p = subparsers.add_parser('mass-offer-circuit')
    mass_offer_circuit_p.add_argument('--wallet', default=None)
    mass_offer_circuit_p.add_argument('--chain', default='bsc')
    mass_offer_circuit_p.add_argument('--window-hours', type=int, default=None)
    mass_offer_circuit_p.add_argument('--limit', type=int, default=5)
    mass_offer_circuit_p.add_argument('--write', action='store_true')
    mass_offer_circuit_p.add_argument('--text', action='store_true')
    mass_offer_quarantine_p = subparsers.add_parser('mass-offer-quarantine')
    mass_offer_quarantine_p.add_argument('--wallet', default=None)
    mass_offer_quarantine_p.add_argument('--chain', default='bsc')
    mass_offer_quarantine_p.add_argument('--window-days', type=int, default=None)
    mass_offer_quarantine_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_quarantine_p.add_argument('--price', type=float, default=None)
    mass_offer_quarantine_p.add_argument('--limit', type=int, default=5)
    mass_offer_quarantine_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_quarantine_p.add_argument('--write', action='store_true')
    mass_offer_quarantine_p.add_argument('--text', action='store_true')
    mass_offer_rebalance_p = subparsers.add_parser('mass-offer-rebalance')
    mass_offer_rebalance_p.add_argument('--wallet', default=None)
    mass_offer_rebalance_p.add_argument('--chain', default='bsc')
    mass_offer_rebalance_p.add_argument('--window-days', type=int, default=None)
    mass_offer_rebalance_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_rebalance_p.add_argument('--price', type=float, default=None)
    mass_offer_rebalance_p.add_argument('--limit', type=int, default=5)
    mass_offer_rebalance_p.add_argument('--policy-limit', type=int, default=None)
    mass_offer_rebalance_p.add_argument('--write', action='store_true')
    mass_offer_rebalance_p.add_argument('--text', action='store_true')
    mass_offer_unwind_p = subparsers.add_parser('mass-offer-unwind')
    mass_offer_unwind_p.add_argument('--wallet', default=None)
    mass_offer_unwind_p.add_argument('--chain', default='bsc')
    mass_offer_unwind_p.add_argument('--window-days', type=int, default=None)
    mass_offer_unwind_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_unwind_p.add_argument('--target-bnb', type=float, default=None)
    mass_offer_unwind_p.add_argument('--max-cancels', type=int, default=None)
    mass_offer_unwind_p.add_argument('--limit', type=int, default=5)
    mass_offer_unwind_p.add_argument('--write', action='store_true')
    mass_offer_unwind_p.add_argument('--apply', action='store_true')
    mass_offer_unwind_p.add_argument('--dry-run', action='store_true')
    mass_offer_unwind_p.add_argument('--text', action='store_true')

    mass_offer_plan_p = subparsers.add_parser('mass-offer-plan')
    mass_offer_plan_p.add_argument('--wallet', default=None)
    mass_offer_plan_p.add_argument('--chain', default='bsc')
    mass_offer_plan_p.add_argument('--window-days', type=int, default=None)
    mass_offer_plan_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_plan_p.add_argument('--price', type=float, default=None)
    mass_offer_plan_p.add_argument('--limit', type=int, default=5)
    mass_offer_plan_p.add_argument('--write', action='store_true')
    mass_offer_plan_p.add_argument('--text', action='store_true')
    mass_offer_batch_p = subparsers.add_parser('mass-offer-batch')
    mass_offer_batch_p.add_argument('--wallet', default=None)
    mass_offer_batch_p.add_argument('--chain', default='bsc')
    mass_offer_batch_p.add_argument('--window-days', type=int, default=None)
    mass_offer_batch_p.add_argument('--event-limit', type=int, default=None)
    mass_offer_batch_p.add_argument('--price', type=float, default=None)
    mass_offer_batch_p.add_argument('--collections', type=int, default=None)
    mass_offer_batch_p.add_argument('--include-dry-run', action='store_true')
    mass_offer_batch_p.add_argument('--dry-run', dest='dry_run', action='store_const', const=True, default=None)
    mass_offer_batch_p.add_argument('--live', dest='dry_run', action='store_const', const=False)
    mass_offer_batch_p.add_argument('--write', action='store_true')
    mass_offer_batch_p.add_argument('--text', action='store_true')
    mass_offer_capital_p = subparsers.add_parser('mass-offer-capital')
    mass_offer_capital_p.add_argument('--chain', default='bsc')
    mass_offer_capital_p.add_argument('--limit', type=int, default=5)
    mass_offer_capital_p.add_argument('--text', action='store_true')
    subparsers.add_parser('compare-markets')
    detect_spreads_p = subparsers.add_parser('detect-spreads')
    detect_spreads_p.add_argument('--min-pct', type=float, default=3.0)
    detect_spreads_p.add_argument('--limit', type=int, default=10)
    detect_spreads_p.add_argument('--sample-limit', type=int, default=5000)
    rank_collections_p = subparsers.add_parser('rank-collections')
    rank_collections_p.add_argument('--min-pct', type=float, default=3.0)
    rank_collections_p.add_argument('--limit', type=int, default=10)
    rank_collections_p.add_argument('--sample-limit', type=int, default=5000)
    wallet_pnl = subparsers.add_parser('wallet-pnl', help='Build wallet portfolio PnL summary from stored events')
    wallet_pnl.add_argument('--wallet', default=None)
    wallet_pnl.add_argument('--limit', type=int, default=5)
    wallet_pnl.add_argument('--write', action='store_true')
    wallet_pnl.add_argument('--text', action='store_true')
    execution_fills = subparsers.add_parser('execution-fills', help='Reconcile execution submits with wallet buy fills')
    execution_fills.add_argument('--wallet', default=None)
    execution_fills.add_argument('--limit', type=int, default=5)
    execution_fills.add_argument('--write', action='store_true')
    execution_fills.add_argument('--text', action='store_true')
    portfolio_risk = subparsers.add_parser('portfolio-risk', help='Assess portfolio/execution risk and optionally enforce dry-run guardrails')
    portfolio_risk.add_argument('--wallet', default=None)
    portfolio_risk.add_argument('--limit', type=int, default=5)
    portfolio_risk.add_argument('--write', action='store_true')
    portfolio_risk.add_argument('--apply-guardrails', action='store_true')
    portfolio_risk.add_argument('--text', action='store_true')
    pnl_guard = subparsers.add_parser('pnl-guard', help='Assess recent realized PnL guardrails and optionally enforce dry-run')
    pnl_guard.add_argument('--wallet', default=None)
    pnl_guard.add_argument('--limit', type=int, default=5)
    pnl_guard.add_argument('--write', action='store_true')
    pnl_guard.add_argument('--apply-guardrails', action='store_true')
    pnl_guard.add_argument('--text', action='store_true')

    execution_health = subparsers.add_parser('execution-health', help='Assess recent execution failure health and optionally enforce dry-run')
    execution_health.add_argument('--limit', type=int, default=5)
    execution_health.add_argument('--write', action='store_true')
    execution_health.add_argument('--apply-guardrails', action='store_true')
    execution_health.add_argument('--text', action='store_true')

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
    if args.command == 'mass-offer-policy-preview':
        return cmd_mass_offer_policy_preview(
            collection=args.collection,
            chain=args.chain,
            price=args.price,
            max_offers=args.max_offers,
            delay_seconds=args.delay_seconds,
            max_existing_offer=args.max_existing_offer,
            dry_run=args.dry_run,
            as_text=args.text,
        )
    if args.command == 'mass-offer-economics':
        return cmd_mass_offer_economics(
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )
    if args.command == 'mass-offer-allocator':
        return cmd_mass_offer_allocator(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )
    if args.command == 'mass-offer-feedback':
        return cmd_mass_offer_feedback(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )
    if args.command == 'mass-offer-budget':
        return cmd_mass_offer_budget(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            price=args.price,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )
    if args.command == 'mass-offer-circuit':
        return cmd_mass_offer_circuit(
            wallet=args.wallet,
            chain=args.chain,
            window_hours=args.window_hours,
            limit=args.limit,
            write=args.write,
            as_text=args.text,
        )
    if args.command == 'mass-offer-quarantine':
        return cmd_mass_offer_quarantine(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            price=args.price,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )

    if args.command == 'mass-offer-rebalance':
        return cmd_mass_offer_rebalance(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            price=args.price,
            policy_limit=args.policy_limit,
            as_text=args.text,
        )

    if args.command == 'mass-offer-unwind':
        return cmd_mass_offer_unwind(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            target_bnb=args.target_bnb,
            max_cancels=args.max_cancels,
            apply=args.apply,
            dry_run=args.dry_run,
            as_text=args.text,
        )

    if args.command == 'mass-offer-plan':
        return cmd_mass_offer_plan(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            limit=args.limit,
            write=args.write,
            price=args.price,
            as_text=args.text,
        )
    if args.command == 'mass-offer-batch':
        return cmd_mass_offer_batch(
            wallet=args.wallet,
            chain=args.chain,
            window_days=args.window_days,
            event_limit=args.event_limit,
            collection_limit=args.collections,
            write=args.write,
            price=args.price,
            dry_run=args.dry_run,
            include_dry_run=args.include_dry_run,
            as_text=args.text,
        )
    if args.command == 'mass-offer-capital':
        return cmd_mass_offer_capital(
            chain=args.chain,
            limit=args.limit,
            as_text=args.text,
        )
    if args.command == 'wallet-pnl':
        return cmd_wallet_pnl(
            wallet=args.wallet,
            limit=args.limit,
            write=args.write,
            as_text=args.text,
        )
    if args.command == 'execution-fills':
        return cmd_execution_fills(
            wallet=args.wallet,
            limit=args.limit,
            write=args.write,
            as_text=args.text,
        )
    if args.command == 'portfolio-risk':
        return cmd_portfolio_risk(
            wallet=args.wallet,
            limit=args.limit,
            write=args.write,
            apply_guardrails=args.apply_guardrails,
            as_text=args.text,
        )
    if args.command == 'pnl-guard':
        return cmd_pnl_guard(
            wallet=args.wallet,
            limit=args.limit,
            write=args.write,
            apply_guardrails=args.apply_guardrails,
            as_text=args.text,
        )
    if args.command == 'execution-health':
        return cmd_execution_health(
            limit=args.limit,
            write=args.write,
            apply_guardrails=args.apply_guardrails,
            as_text=args.text,
        )
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
