from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import sqlite3
from datetime import datetime, timezone
from urllib import parse

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
from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid import CounterBidTask, CounterBidder, CounterbidConfigManager
from okx_nft_bot.counterbid.okx_api import OKXAPIClient
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
    MassOfferBudgetRebalancer,
    MassOfferBudgetScheduler,
    MassOfferCircuitBreaker,
    MassOfferQuarantineController,
    MassOfferUnwindController,
    MassOfferEconomics,
    MassOfferEngine,
    MassOfferFeedbackController,
    MassOfferPlanner,
    MassOfferRunResult,
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
    get_mass_offer_batch_runtime_summary,
    get_mass_offer_budget_runtime_summary,
    get_mass_offer_circuit_runtime_summary,
    get_mass_offer_quarantine_runtime_summary,
    get_mass_offer_rebalance_runtime_summary,
    get_mass_offer_unwind_runtime_summary,
)
from okx_nft_bot.ops import (
    acknowledge_health_alert,
    get_health_alert_control,
    is_alertable_health_result,
    reset_health_alert_control,
    run_healthcheck,
    snooze_health_alerts,
    write_runtime_metrics,
)
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter import PositionState, UndercutEngine

logger = logging.getLogger(__name__)


def _format_money_breakdown(values: dict[str, float], *, signed: bool = False) -> str:
    if not values:
        return 'n/a'
    parts: list[str] = []
    for currency, amount in sorted(values.items()):
        template = f'{float(amount):+.6f}' if signed else f'{float(amount):.6f}'
        parts.append(f'{currency}:{template}')
    return ', '.join(parts)


@dataclass(slots=True)
class TelegramBotClient:
    bot_token: str
    transport: StdlibHttpTransport

    def _base(self) -> str:
        return f'https://api.telegram.org/bot{self.bot_token}'

    def get_updates(self, *, offset: int | None = None, timeout: int = 10) -> dict[str, object]:
        params = {'timeout': timeout}
        if offset is not None:
            params['offset'] = offset
        body = parse.urlencode(params)
        return self.transport.request_json(
            method='POST',
            url=f'{self._base()}/getUpdates',
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            body=body,
        )

    def send_message(self, *, chat_id: str, text: str) -> dict[str, object]:
        body = parse.urlencode({'chat_id': chat_id, 'text': text})
        return self.transport.request_json(
            method='POST',
            url=f'{self._base()}/sendMessage',
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            body=body,
        )


class TelegramCommandProcessor:
    def __init__(self, *, settings: Settings, store: SQLiteStore, registry: CollectionRegistry, runner: MultiCollectionRunner, client: TelegramBotClient) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.runner = runner
        self.client = client
        self.namespace = 'telegram_bot'
        self.parasite_hunter = None  # set externally by sales_stream or cli

    def poll_once(self) -> dict[str, int]:
        offset = self._load_offset()
        payload = self.client.get_updates(offset=offset, timeout=self.settings.telegram_poll_timeout)
        results = payload.get('result', [])
        processed = 0
        latest_offset = offset
        if isinstance(results, list):
            for update in results:
                if not isinstance(update, dict):
                    continue
                update_id = int(update.get('update_id', 0))
                latest_offset = max(latest_offset or 0, update_id + 1)
                message = update.get('message') or update.get('edited_message')
                if not isinstance(message, dict):
                    continue
                text = message.get('text')
                chat = message.get('chat') or {}
                chat_id = str(chat.get('id', ''))
                if not text or not chat_id:
                    continue
                if self._is_blocked(chat_id):
                    continue
                response = self._handle_command(chat_id=chat_id, text=str(text))
                if response:
                    self.client.send_message(chat_id=chat_id, text=response)
                processed += 1
        if latest_offset is not None:
            self._save_offset(latest_offset)
        return {'processed': processed, 'next_offset': latest_offset or 0}

    # ── PATCH 2026-08-01: двухуровневый доступ ──────────────────────
    # ADMIN (Robert)    — всё, включая боевой режим и движение капитала.
    # OPERATOR (помощник) — только наблюдение и безопасные операции.
    # Задаётся через TELEGRAM_OPERATOR_CHAT_IDS (через запятую).
    _OPERATOR_ALLOWED = {
        # наблюдение
        '/help', '/status', '/health', '/exechealth', '/pnl', '/fills',
        '/offers', '/collections', '/latest', '/markets', '/rankings',
        '/sales', '/spreads', '/risk', '/dashboard', '/pnlguard',
        '/undercutstatus', '/massofferstatus', '/backups', '/profiles',
        # разведка по паразитам (чтение)
        '/parasitescan', '/parasitesales', '/parasitelive', '/parasite',
        # управление алертами (безопасно)
        '/alertstatus', '/alertack', '/alertsnooze', '/alertreset',
    }

    def _operator_ids(self) -> set:
        import os as _os
        raw = _os.getenv('TELEGRAM_OPERATOR_CHAT_IDS', '') or ''
        return {x.strip() for x in raw.split(',') if x.strip()}

    def _access_level(self, chat_id: str) -> str:
        """'admin' | 'operator' | 'none'"""
        admins = self.settings.telegram_admin_chat_ids
        if admins and chat_id in admins:
            return 'admin'
        if chat_id in self._operator_ids():
            return 'operator'
        return 'none'

    def _is_blocked(self, chat_id: str) -> bool:
        # fail-closed: нет ни админов, ни операторов — блокируем всех
        if not self.settings.telegram_admin_chat_ids and not self._operator_ids():
            return True
        return self._access_level(chat_id) == 'none' 

    def _load_offset(self) -> int | None:
        raw = self.store.get_state(self.namespace, 'update_offset')
        return int(raw) if raw else None

    def _save_offset(self, offset: int) -> None:
        self.store.set_state(self.namespace, 'update_offset', str(offset))

    def _handle_command(self, *, chat_id: str, text: str) -> str | None:
        parts = text.strip().split()
        if not parts:
            return None
        command = parts[0].split('@', 1)[0].lower()
        args = parts[1:]

        # ── PATCH 2026-08-01: оператору доступен только безопасный набор ──
        if self._access_level(chat_id) == 'operator' and command not in self._OPERATOR_ALLOWED:
            return ("\u26d4 Команда " + command + " доступна только владельцу.\n\n"
                    "Тебе доступно: наблюдение (/status /health /pnl /fills /offers),\n"
                    "разведка (/parasitescan /parasitesales) и алерты (/alertack).\n"
                    "Полный список — /help")

        if command == '/help':
            return self._help_text()
        if command == '/status':
            return self._status_text()
        if command == '/collections':
            return self._collections_text()
        if command == '/latest':
            limit = int(args[0]) if args and args[0].isdigit() else 5
            return self._latest_text(limit=limit)
        if command == '/offers':
            return self._offers_text(args)
        if command == '/counterrun':
            return self._counterrun_command(args)
        if command == '/counterconfig':
            return self._counterconfig_command(args)
        if command == '/massoffer':
            return self._mass_offer_command(args)
        if command == '/massofferstatus':
            return self._mass_offer_status_text(args)
        if command == '/massofferpolicy':
            return self._mass_offer_policy_text(args)
        if command == '/massofferecon':
            return self._mass_offer_economics_text(args)
        if command == '/massofferalloc':
            return self._mass_offer_allocator_text(args)
        if command == '/massofferfeedback':
            return self._mass_offer_feedback_text(args)
        if command == '/massofferbudget':
            return self._mass_offer_budget_text(args)
        if command == '/massofferquarantine':
            return self._mass_offer_quarantine_text(args)
        if command == '/massofferrebalance':
            return self._mass_offer_rebalance_text(args)
        if command == '/massofferunwind':
            return self._mass_offer_unwind_text(args)
        if command == '/massoffercircuit':
            return self._mass_offer_circuit_text(args)
        if command == '/massofferplan':
            return self._mass_offer_plan_text(args)
        if command == '/massofferbatch':
            return self._mass_offer_batch_text(args)
        if command == '/massoffercapital':
            return self._mass_offer_capital_text(args)
        if command == '/massoffercancel':
            return self._mass_offer_cancel_command(args)
        if command == '/undercutstatus':
            return self._undercut_status_text()
        if command == '/dashboard':
            return self._dashboard_command(args)
        if command == '/pnl':
            return self._pnl_text(args)
        if command == '/fills':
            return self._fills_text(args)
        if command == '/risk':
            return self._risk_text(args)
        if command == '/pnlguard':
            return self._pnl_guard_text(args)
        if command == '/exechealth':
            return self._execution_health_text(args)
        if command == '/armlive':
            return self._arm_live_command(args)
        if command == '/disarmlive':
            return self._disarm_live_command(args)
        if command == '/killswitch':
            return self._killswitch_command(args)
        if command == '/markets':
            return self._markets_text()
        if command == '/spreads':
            return self._spreads_text(args)
        if command == '/rankings':
            return self._rankings_text(args)
        if command == '/sendalerts':
            return self._send_alerts_command(args)
        if command == '/run':
            return self._run_command(args)
        if command == '/resetcursor':
            return self._reset_cursor_command(args)
        if command == '/sales':
            return self._sales_stats_text()
        if command == '/parasitesales':
            limit = int(args[0]) if args and args[0].isdigit() else 10
            return self._parasite_sales_text(limit=limit)
        if command == '/parasite':
            return self._parasite_status_text()
        if command == '/parasitescan':
            return self._parasite_scan_command()
        if command == '/parasitelive':
            return self._parasite_live_toggle(args)
        if command == '/health':
            return self._health_text()
        if command == '/writemetrics':
            return self._write_metrics_text()
        if command == '/alertstatus':
            return self._alert_status_text(args)
        if command == '/alertack':
            return self._alert_ack_command(args)
        if command == '/alertsnooze':
            return self._alert_snooze_command(args)
        if command == '/alertreset':
            return self._alert_reset_command(args)
        if command == '/profiles':
            return self._profiles_text()
        if command == '/profile':
            return self._profile_text()
        if command == '/setprofile':
            return self._set_profile_command(args)
        if command == '/backup':
            return self._backup_command(args)
        if command == '/backups':
            return self._backups_text(args)
        if command == '/restore':
            return self._restore_command(args)
        return 'Unknown command. Use /help'

    def _help_text(self) -> str:
        return (
            'Commands:\n'
            '/status - bot status\n'
            '/collections - active registry\n'
            '/offers <okx|opensea> [collection_or_slug] [limit] - stored offers by market\n'
            '/counterrun <collection> - dry-run parasite scan for one execution collection\n'
            '/counterconfig <collection> <min_price> <max_price> [margin] - save execution config\n'
            '/massoffer <collection> <price> [rarity_filter] - run unlisted-only per-item offers\n'
            '/massofferstatus - show latest mass-offer campaigns\n'
            '/massofferpolicy <collection> <price> [max_offers] [delay_seconds] - preview collection policy\n'
            '/massofferecon [window_days] [limit] - summarize collection economics\n'
            '/massofferalloc [window_days] [limit] - PnL-aware allocator bands/policy\n'
            '/massofferfeedback [window_days] [limit] - execution feedback overlay for allocator\n'
            '/massofferbudget [window_days] [limit] [price] - adaptive live budget allocation overlay\n'
            '/massofferrebalance [window_days] [limit] [price] - rebalance overlay from pnl and campaign drift\n'
            '/massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live] - targeted active-offer unwind plan\n'
            '/massoffercircuit [window_hours] [limit] - campaign circuit breaker summary\n'
            '/massofferplan [window_days] [limit] [price] - actionable queue for next mass-offer runs\n'
            '/massofferbatch [window_days] [collections] [price] [dry|live] - execute top queued collections\n'
            '/massoffercapital [limit] - show active exposure and cap headroom\n'
            '/massoffercancel - cancel active per-item mass offers\n'
            '/undercutstatus - show dry-run undercutter status\n'
            '/dashboard - show execution dashboard summary\n'
            '/pnl [limit] - wallet realized/unrealized PnL snapshot\n'
            '/fills [limit] - reconcile execution-confirmed fills\n'
            '/risk [limit] - portfolio/execution risk guard summary\n'
            '/pnlguard [limit] - recent realized PnL guard summary\n'
            '/exechealth [limit] - recent execution failure health summary\n'
            '/armlive [minutes] [reason] - open a short-lived live execution window\n'
            '/disarmlive [reason] - close the current live execution window\n'
            '/killswitch - cancel active execution offers and force dry-run mode\n'
            '/markets - cross-market summary\n'
            '/spreads [min_pct] [limit] - cross-market spreads\n'
            '/rankings [limit] - collection ranking\n'
            '/sendalerts [min_pct] [limit] - deliver analytics report\n'
            '/sales - sales stream stats (all markets)\n'
            '/parasitesales [n] - recent parasite-involved sales\n'
            '/parasite - parasite hunter status & last scan\n'
            '/parasitescan - trigger immediate parasite scan\n'
            '/parasitelive on|off - toggle DRY_RUN/LIVE mode\n'
            '/latest [n] - latest stored events\n'
            '/run <collection|all> [trades|listings] - trigger cycle\n'
            '/resetcursor <collection> [trades|listings] - clear cursor\n'
            '/health - runtime healthcheck\n'
            '/writemetrics - write metrics snapshot\n'
            '/alertstatus - show health alert ack/snooze state\n'
            '/alertack [note] - acknowledge the current alertable health issue\n'
            '/alertsnooze [minutes] [reason] - suppress health alerts temporarily\n'
            '/alertreset - clear health alert ack/snooze state\n'
            '/profiles - available deploy profiles\n'
            '/profile - runtime + desired profile\n'
            '/setprofile <dev|stage|prod> - set desired profile for next restart\n'
            '/backup [label] - create DB backup\n'
            '/backups [n] - list recent backups\n'
            '/restore <filename> - restore DB from backup (creates safety backup)\n'
            '/help - show help'
        )

    def _status_text(self) -> str:
        latest = self.store.fetch_latest_events(limit=3)
        return (
            f'env={self.settings.app_env}\n'
            f'profile={self.settings.app_profile}\n'
            f'db={self.settings.db_path}\n'
            f'collections={len(self.registry.active())}\n'
            f'events={self.store.count_events()}\n'
            f'notifications={self.store.count_notifications()}\n'
            f'latest={json.dumps(latest, ensure_ascii=False)}'
        )

    def _collections_text(self) -> str:
        lines = ['Active collections:']
        for target in self.registry.active():
            lines.append(f'- {target.name} [{target.market}:{",".join(target.source_modes)}]')
        return '\n'.join(lines)

    def _markets_text(self) -> str:
        rows = self.store.fetch_market_summary()
        if not rows:
            return 'No market data stored yet'
        return 'Market summary:\n' + '\n'.join(
            f"- {row['market']} | {row['collection_name']} | events={row['event_count']} | floor={row['floor_price']} | avg={row['avg_price']}"
            for row in rows[:10]
        )

    def _latest_text(self, *, limit: int) -> str:
        rows = self.store.fetch_latest_events(limit=limit)
        if not rows:
            return 'No events stored yet'
        lines = []
        for row in rows:
            lines.append(f"{row['event_time']} | {row['market']} | {row['collection_name']} #{row['token_id']} | {row.get('price')} {row.get('currency')}")
        return '\n'.join(lines)

    def _offers_text(self, args: list[str]) -> str:
        if not args:
            return 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'
        market = args[0].strip().lower()
        if market not in {'okx', 'opensea'}:
            return 'Unknown market. Use okx or opensea'

        if len(args) > 3:
            return 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'

        collection_filter: str | None = None
        limit = 5

        if len(args) > 1:
            if args[1].isdigit():
                if int(args[1]) <= 0 or len(args) > 2:
                    return 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'
                limit = int(args[1])
            else:
                collection_filter = args[1]
                if len(args) > 2:
                    if not args[2].isdigit() or int(args[2]) <= 0:
                        return 'Usage: /offers <okx|opensea> [collection_or_slug] [limit]'
                    limit = int(args[2])

        offers_store = OffersStore(self.settings.offers_db_path)
        offers = offers_store.query_offers(OfferFilters(market=market, collection=collection_filter, limit=limit))
        if not offers:
            if collection_filter:
                return f'No stored offers for market={market} collection={collection_filter}'
            return f'No stored offers for market={market}'

        header = f'Stored offers for {market}:'
        if collection_filter:
            header = f'Stored offers for {market} collection={collection_filter}:'
        lines = [header]
        for offer in offers:
            price = f'{offer.price:.6f}' if offer.price is not None else 'n/a'
            token = f' #{offer.token_id}' if offer.token_id else ''
            lines.append(
                f"- {offer.collection_slug_or_address}{token} | {price} {offer.currency or ''} | {offer.status}"
            )
        return '\n'.join(lines)

    def _counterrun_command(self, args: list[str]) -> str:
        if len(args) != 1:
            return 'Usage: /counterrun <collection>'
        bidder = CounterBidder(settings=self.settings)
        result = bidder.process_batch(
            chain=self.settings.execution_chain,
            refresh=False,
            sign_preview=False,
            collection=args[0],
        )
        if not result.tasks:
            return 'counterbid_scan\nno tasks returned'
        task = result.tasks[0]
        return self._format_counterbid_task(task)

    def _counterconfig_command(self, args: list[str]) -> str:
        if len(args) not in {3, 4}:
            return 'Usage: /counterconfig <collection> <min_price> <max_price> [margin]'
        try:
            min_price = float(args[1])
            max_price = float(args[2])
            margin = float(args[3]) if len(args) == 4 else 0.001
        except ValueError:
            return 'Usage: /counterconfig <collection> <min_price> <max_price> [margin]'

        manager = CounterbidConfigManager(self.settings.execution_db_path)
        config = manager.add_collection(
            address=args[0],
            chain=self.settings.execution_chain,
            min_price_bnb=min_price,
            max_price_bnb=max_price,
            margin_bnb=margin,
        )
        return (
            'counterbid_config saved\n'
            f'collection={config.address}\n'
            f'chain={config.chain}\n'
            f'min_price_bnb={config.min_price_bnb:.6f}\n'
            f'max_price_bnb={config.max_price_bnb:.6f}\n'
            f'margin_bnb={config.margin_bnb:.6f}\n'
            f'enabled={config.enabled}'
        )

    def _mass_offer_command(self, args: list[str]) -> str:
        if len(args) not in {2, 3}:
            return 'Usage: /massoffer <collection> <price> [rarity_filter]'
        try:
            price = float(args[1])
        except ValueError:
            return 'Usage: /massoffer <collection> <price> [rarity_filter]'

        rarity_filter = [part.strip() for part in args[2].split(',')] if len(args) == 3 else []
        engine = MassOfferEngine(settings=self.settings)
        result = engine.run(
            collection=args[0],
            chain=self.settings.execution_chain,
            price_bnb=price,
            rarity_filter=rarity_filter,
            unlisted_only=True,
        )
        return self._format_mass_offer_result(result)

    def _mass_offer_status_text(self, args: list[str]) -> str:
        if args:
            return 'Usage: /massofferstatus'
        engine = MassOfferEngine(settings=self.settings)
        payload = engine.status(chain=self.settings.execution_chain)
        campaigns = payload.get('campaigns', [])
        lines = [
            'mass_offer_status',
            f"chain={payload.get('chain')}",
            f"effective_dry_run={payload.get('effective_dry_run')}",
            f"active_offers={payload.get('active_offer_count', 0)}",
            f"campaigns={len(campaigns)}",
            f"policy_entries={payload.get('policy_entries', 0)}",
        ]
        if campaigns:
            latest = campaigns[0]
            lines.extend(
                [
                    f"latest_campaign_id={latest['campaign_id']}",
                    f"latest_collection={latest['collection']}",
                    f"latest_status={latest['status']}",
                    f"latest_targets={latest['target_count']}",
                    f"latest_submitted={latest['submitted_count']}",
                    f"latest_dry_run={latest['dry_run_count']}",
                    f"latest_skipped={latest['skipped_count']}",
                    f"latest_failed={latest['failed_count']}",
                ]
            )
        for offer in payload.get('active_offers', [])[:5]:
            lines.append(
                f"- #{offer['token_id']} @ {float(offer['price_bnb']):.6f} BNB [{offer['status']}]"
            )
        return '\n'.join(lines)

    def _mass_offer_policy_text(self, args: list[str]) -> str:
        if len(args) not in {2, 3, 4}:
            return 'Usage: /massofferpolicy <collection> <price> [max_offers] [delay_seconds]'
        try:
            price = float(args[1])
            max_offers = int(args[2]) if len(args) >= 3 else None
            delay_seconds = float(args[3]) if len(args) >= 4 else None
        except ValueError:
            return 'Usage: /massofferpolicy <collection> <price> [max_offers] [delay_seconds]'
        engine = MassOfferEngine(settings=self.settings)
        payload = engine.preview_policy(
            collection=args[0],
            chain=self.settings.execution_chain,
            price_bnb=price,
            max_total=max_offers,
            delay_seconds=delay_seconds,
        )
        return format_mass_offer_policy_preview(payload)

    def _mass_offer_economics_text(self, args: list[str]) -> str:
        if len(args) > 2:
            return 'Usage: /massofferecon [window_days] [limit]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_economics_window_days
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferecon [window_days] [limit]'
        economics = MassOfferEconomics(settings=self.settings, store=self.store)
        report = economics.build_report(
            chain=self.settings.execution_chain,
            window_days=window_days,
            event_limit=self.settings.mass_offer_economics_event_limit,
        )
        return format_mass_offer_economics_text(report, limit=limit)

    def _mass_offer_allocator_text(self, args: list[str]) -> str:
        if len(args) > 2:
            return 'Usage: /massofferalloc [window_days] [limit]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_allocator_window_days
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferalloc [window_days] [limit]'
        allocator = MassOfferAllocator(settings=self.settings, store=self.store)
        report = allocator.build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
        )
        return format_mass_offer_allocator_text(report, limit=limit)

    def _mass_offer_feedback_text(self, args: list[str]) -> str:
        if len(args) > 2:
            return 'Usage: /massofferfeedback [window_days] [limit]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_feedback_window_days
        except ValueError:
            return 'Usage: /massofferfeedback [window_days] [limit]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferfeedback [window_days] [limit]'
        report = MassOfferFeedbackController(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
        )
        return format_mass_offer_feedback_text(report, limit=limit)


    def _mass_offer_budget_text(self, args: list[str]) -> str:
        if len(args) > 3:
            return 'Usage: /massofferbudget [window_days] [limit] [price]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_allocator_window_days
        except ValueError:
            return 'Usage: /massofferbudget [window_days] [limit] [price]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferbudget [window_days] [limit] [price]'
        try:
            price = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_price_bnb
        except ValueError:
            return 'Usage: /massofferbudget [window_days] [limit] [price]'
        report = MassOfferBudgetScheduler(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            price_bnb=price,
        )
        return format_mass_offer_budget_text(report, limit=limit)

    def _mass_offer_quarantine_text(self, args: list[str]) -> str:
        if len(args) > 3:
            return 'Usage: /massofferquarantine [window_days] [limit] [price]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_quarantine_window_days
        except ValueError:
            return 'Usage: /massofferquarantine [window_days] [limit] [price]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferquarantine [window_days] [limit] [price]'
        try:
            price = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_price_bnb
        except ValueError:
            return 'Usage: /massofferquarantine [window_days] [limit] [price]'
        report = MassOfferQuarantineController(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            price_bnb=price,
        )
        return format_mass_offer_quarantine_text(report, limit=limit)


    def _mass_offer_rebalance_text(self, args: list[str]) -> str:
        if len(args) > 3:
            return 'Usage: /massofferrebalance [window_days] [limit] [price]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_rebalance_window_days
        except ValueError:
            return 'Usage: /massofferrebalance [window_days] [limit] [price]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferrebalance [window_days] [limit] [price]'
        try:
            price = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_price_bnb
        except ValueError:
            return 'Usage: /massofferrebalance [window_days] [limit] [price]'
        report = MassOfferBudgetRebalancer(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            price_bnb=price,
        )
        return format_mass_offer_rebalance_text(report, limit=limit)


    def _mass_offer_unwind_text(self, args: list[str]) -> str:
        if len(args) > 4:
            return 'Usage: /massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_unwind_window_days
        except ValueError:
            return 'Usage: /massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live]'
        try:
            limit = int(args[1]) if len(args) >= 2 else self.settings.mass_offer_unwind_max_cancels
        except ValueError:
            return 'Usage: /massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live]'
        try:
            target_bnb = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_unwind_target_release_bnb
        except ValueError:
            return 'Usage: /massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live]'
        mode = str(args[3]).strip().lower() if len(args) >= 4 else 'preview'
        if mode not in {'preview', 'dry', 'live'}:
            return 'Usage: /massofferunwind [window_days] [limit] [target_bnb] [preview|dry|live]'
        controller = MassOfferUnwindController(settings=self.settings, store=self.store)
        report = controller.build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            target_release_bnb=target_bnb,
            max_cancels=limit,
        )
        text = format_mass_offer_unwind_text(report, limit=limit)
        if mode == 'preview':
            return text
        execution = controller.execute_report(report, dry_run=(mode != 'live'))
        return f"{text}\n\n{format_mass_offer_unwind_execution_text(execution)}"


    def _mass_offer_circuit_text(self, args: list[str]) -> str:
        if len(args) > 2:
            return 'Usage: /massoffercircuit [window_hours] [limit]'
        try:
            window_hours = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_circuit_window_hours
        except ValueError:
            return 'Usage: /massoffercircuit [window_hours] [limit]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massoffercircuit [window_hours] [limit]'
        report = MassOfferCircuitBreaker(settings=self.settings).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_hours=window_hours,
        )
        return format_mass_offer_circuit_text(report, limit=limit)


    def _mass_offer_plan_text(self, args: list[str]) -> str:
        if len(args) > 3:
            return 'Usage: /massofferplan [window_days] [limit] [price]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_allocator_window_days
        except ValueError:
            return 'Usage: /massofferplan [window_days] [limit] [price]'
        try:
            limit = int(args[1]) if len(args) >= 2 else 5
        except ValueError:
            return 'Usage: /massofferplan [window_days] [limit] [price]'
        try:
            price = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_price_bnb
        except ValueError:
            return 'Usage: /massofferplan [window_days] [limit] [price]'
        report = MassOfferPlanner(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            price_bnb=price,
        )
        return format_mass_offer_plan_text(report, limit=limit)

    def _mass_offer_batch_text(self, args: list[str]) -> str:
        if len(args) > 4:
            return 'Usage: /massofferbatch [window_days] [collections] [price] [dry|live]'
        try:
            window_days = int(args[0]) if len(args) >= 1 else self.settings.mass_offer_allocator_window_days
        except ValueError:
            return 'Usage: /massofferbatch [window_days] [collections] [price] [dry|live]'
        try:
            collection_limit = int(args[1]) if len(args) >= 2 else self.settings.mass_offer_batch_collection_limit
        except ValueError:
            return 'Usage: /massofferbatch [window_days] [collections] [price] [dry|live]'
        try:
            price = float(args[2]) if len(args) >= 3 else self.settings.mass_offer_price_bnb
        except ValueError:
            return 'Usage: /massofferbatch [window_days] [collections] [price] [dry|live]'
        mode = args[3].strip().lower() if len(args) >= 4 else 'auto'
        if mode not in {'auto', 'dry', 'live'}:
            return 'Usage: /massofferbatch [window_days] [collections] [price] [dry|live]'
        dry_run = None if mode == 'auto' else (mode == 'dry')
        runner = MassOfferBatchRunner(settings=self.settings, store=self.store)
        report = runner.run_batch(
            wallet=self.settings.buyer_wallet_address,
            chain=self.settings.execution_chain,
            window_days=window_days,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            event_limit=self.settings.mass_offer_economics_event_limit,
            price_bnb=price,
            collection_limit=collection_limit,
            dry_run=dry_run,
        )
        return format_mass_offer_batch_text(report, limit=collection_limit)

    def _mass_offer_capital_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /massoffercapital [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /massoffercapital [limit]'
        engine = MassOfferEngine(settings=self.settings)
        payload = engine.capital_status(chain=self.settings.execution_chain, limit=limit)
        return format_mass_offer_capital_text(payload)

    def _mass_offer_cancel_command(self, args: list[str]) -> str:
        if args:
            return 'Usage: /massoffercancel'
        engine = MassOfferEngine(settings=self.settings)
        payload = engine.cancel_active(chain=self.settings.execution_chain)
        return (
            'mass_offer_cancel\n'
            f"chain={payload['chain']}\n"
            f"active_seen={payload['active_seen']}\n"
            f"cancelled={payload['cancelled']}\n"
            f"failed={len(payload['failed'])}"
        )

    def _undercut_status_text(self) -> str:
        engine = UndercutEngine(settings=self.settings)
        status = engine.status(chain=self.settings.execution_chain)
        lines = [
            'undercut_status',
            f"chain={status['chain']}",
            f"dry_run={status['dry_run']}",
            f"active_offers={len(status.get('active_offers', []))}",
            f"recent_actions={len(status.get('recent_actions', []))}",
            f"tracked_collections={len(status.get('tracked_collections', []))}",
        ]
        for offer in status.get('active_offers', [])[:5]:
            lines.append(
                f"- {offer['collection']} @ {float(offer['price_bnb']):.6f} BNB [{offer['status']}]"
            )
        return '\n'.join(lines)

    def _dashboard_command(self, args: list[str]) -> str:
        if args:
            return 'Usage: /dashboard'
        state = PositionState(self.settings.execution_db_path)
        integrity = state.audit_integrity().to_dict()
        now = datetime.now(timezone.utc)
        runtime = state.get_runtime_state()
        arm_state = state.get_live_arm_state(now=now)
        if state.is_force_dry_run():
            mode = 'DRY-RUN (FORCED)'
        elif self.settings.dry_run:
            mode = 'DRY-RUN'
        elif arm_state['armed']:
            mode = 'LIVE (ARMED)'
        else:
            mode = 'LIVE (UNARMED BLOCKED)'
        summary = state.get_today_action_summary(now=now, chain=self.settings.execution_chain)
        try:
            submit_count = state.get_today_submit_count(now=now, chain=self.settings.execution_chain)
        except Exception as exc:
            logger.warning("Dashboard submit-count lookup failed: %s", exc)
            submit_count = None
        hourly = state.get_hourly_submit_count(now=now, chain=self.settings.execution_chain)
        active_count = len(state.get_active_offers(chain=self.settings.execution_chain))
        tracked_count = state.get_tracked_collections_count(chain=self.settings.execution_chain)
        parasites = self._parasite_detected_count()
        parasites_text = str(parasites) if parasites is not None else 'N/A'
        reconcile_at = runtime.get('last_reconcile_at', 'never')
        arm_text = f"yes ({arm_state['minutes_remaining']}m left)" if arm_state['armed'] else 'no'
        integrity_text = 'OK' if integrity['ok'] else (
            f"issues={integrity['issue_count']}, quarantined={integrity['quarantine_count']}"
        )
        fill_summary = state.get_fill_summary(chain=self.settings.execution_chain)
        fill_reconcile_at = runtime.get('last_fill_reconcile_at', 'never')
        risk_summary = None
        try:
            risk_report = PortfolioRiskAnalyzer(settings=self.settings, store=self.store, state=state).build_report(
                wallet=self.settings.buyer_wallet_address,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                chain=self.settings.execution_chain,
            )
            risk_summary = risk_report.summary
        except Exception as exc:
            logger.warning("Dashboard portfolio risk unavailable: %s", exc)
        allocator_summary = None
        try:
            allocator_report = MassOfferAllocator(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_allocator_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
            )
            allocator_summary = allocator_report.summary
        except Exception as exc:
            logger.warning("Dashboard allocator summary unavailable: %s", exc)
        plan_summary = None
        try:
            plan_report = MassOfferPlanner(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_allocator_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
                price_bnb=self.settings.mass_offer_price_bnb,
            )
            plan_summary = plan_report.summary
        except Exception as exc:
            logger.warning("Dashboard plan summary unavailable: %s", exc)
        budget_summary = None
        try:
            budget_report = MassOfferBudgetScheduler(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_allocator_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
                price_bnb=self.settings.mass_offer_price_bnb,
            )
            budget_summary = budget_report.summary
        except Exception as exc:
            logger.warning("Dashboard budget summary unavailable: %s", exc)
        quarantine_summary = None
        try:
            quarantine_report = MassOfferQuarantineController(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_quarantine_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
                price_bnb=self.settings.mass_offer_price_bnb,
            )
            quarantine_summary = quarantine_report.summary
        except Exception as exc:
            logger.warning("Dashboard quarantine summary unavailable: %s", exc)
        rebalance_summary = None
        try:
            rebalance_report = MassOfferBudgetRebalancer(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_rebalance_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
                price_bnb=self.settings.mass_offer_price_bnb,
            )
            rebalance_summary = rebalance_report.summary
        except Exception as exc:
            logger.warning("Dashboard rebalance summary unavailable: %s", exc)
        unwind_summary = None
        try:
            unwind_report = MassOfferUnwindController(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                chain=self.settings.execution_chain,
                window_days=self.settings.mass_offer_unwind_window_days,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                event_limit=self.settings.mass_offer_economics_event_limit,
                target_release_bnb=self.settings.mass_offer_unwind_target_release_bnb,
                max_cancels=self.settings.mass_offer_unwind_max_cancels,
            )
            unwind_summary = unwind_report.summary
        except Exception as exc:
            logger.warning("Dashboard unwind summary unavailable: %s", exc)
        batch_summary = get_mass_offer_batch_runtime_summary(state)
        budget_runtime = get_mass_offer_budget_runtime_summary(state)
        quarantine_runtime = get_mass_offer_quarantine_runtime_summary(state)
        rebalance_runtime = get_mass_offer_rebalance_runtime_summary(state)
        unwind_runtime = get_mass_offer_unwind_runtime_summary(state)
        circuit_summary = get_mass_offer_circuit_runtime_summary(state)
        lines = [
            'NFT Bot Dashboard',
            '------------------',
            f'Mode:          {mode}',
            f'Live Armed:    {arm_text}',
            f'Integrity:     {integrity_text}',
            f'Active Offers: {active_count}',
            '------------------',
            f"Today Actions: {summary['total']} ({summary['attacks']} attacks, {summary['withdraws']} withdraws)"
            f"{f' [submits: {submit_count}]' if submit_count is not None else ''}",
            f"BNB Spent:     {summary['bnb_spent']:.4f} / {self.settings.max_bnb_per_day:.4f} limit",
            f'Rate:          {hourly} / {self.settings.max_live_offers_per_hour} per hour',
            f'Reconciled:    {reconcile_at}',
            f"Confirmed:     {fill_summary.get('confirmed_fill_count', 0)} fills @ {fill_reconcile_at}",
        ]
        if risk_summary is not None:
            risk_line = f"Risk:          {risk_summary.severity}"
            if risk_summary.top_breach_code:
                risk_line += f" [{risk_summary.top_breach_code}]"
            lines.append(risk_line)
        pnl_guard_summary = None
        try:
            pnl_guard_report = PnlGuardAnalyzer(settings=self.settings, store=self.store).build_report(
                wallet=self.settings.buyer_wallet_address,
                reference_limit=self.settings.wallet_pnl_reference_event_limit,
                chain=self.settings.execution_chain,
                window_hours=self.settings.pnl_guard_window_hours,
            )
            pnl_guard_summary = pnl_guard_report.summary
        except Exception as exc:
            logger.warning("Dashboard pnl-guard summary unavailable: %s", exc)
        if pnl_guard_summary is not None:
            pnl_guard_line = f"PnL Guard:     {pnl_guard_summary.severity}"
            if pnl_guard_summary.top_breach_code:
                pnl_guard_line += f" [{pnl_guard_summary.top_breach_code}]"
            if pnl_guard_summary.realized_pnl_native is not None:
                pnl_guard_line += f" pnl={pnl_guard_summary.realized_pnl_native:+.4f} {pnl_guard_summary.currency}"
            lines.append(pnl_guard_line)
        execution_health_summary = None
        try:
            execution_health_report = ExecutionHealthAnalyzer(settings=self.settings).build_report(
                chain=self.settings.execution_chain,
                window_hours=self.settings.execution_health_window_hours,
                event_limit=self.settings.execution_health_event_limit,
            )
            execution_health_summary = execution_health_report.summary
        except Exception as exc:
            logger.warning("Dashboard execution-health summary unavailable: %s", exc)
        if execution_health_summary is not None:
            execution_health_line = f"Exec Health:   {execution_health_summary.severity}"
            if execution_health_summary.top_issue_code:
                execution_health_line += f" [{execution_health_summary.top_issue_code}]"
            execution_health_line += f" fail={execution_health_summary.failed_count}/{execution_health_summary.attempt_count}"
            lines.append(execution_health_line)
        if circuit_summary is not None:
            circuit_line = f"Circuit:       {str(circuit_summary.get('severity') or 'ok').upper()}"
            if circuit_summary.get('issue_code'):
                circuit_line += f" [{circuit_summary.get('issue_code')}]"
            if circuit_summary.get('top_collection'):
                circuit_line += f" top={circuit_summary.get('top_collection')}"
            lines.append(circuit_line)
        if allocator_summary is not None:
            alloc_line = (
                f"Allocator:     OW={allocator_summary.get('overweight_count', 0)} "
                f"N={allocator_summary.get('neutral_count', 0)} "
                f"UW={allocator_summary.get('underweight_count', 0)} "
                f"W={allocator_summary.get('watch_count', 0)} "
                f"B={allocator_summary.get('block_count', 0)}"
            )
            lines.append(alloc_line)
        if plan_summary is not None:
            feedback_line = (
                f"Feedback:      P={plan_summary.get('feedback_promote_count', 0)} "
                f"S={plan_summary.get('feedback_steady_count', 0)} "
                f"T={plan_summary.get('feedback_throttle_count', 0)} "
                f"W={plan_summary.get('feedback_watch_count', 0)} "
                f"X={plan_summary.get('feedback_pause_count', 0)}"
            )
            lines.append(feedback_line)
        if plan_summary is not None:
            plan_line = (
                f"Plan:          ready={plan_summary.get('ready_count', 0)} "
                f"dry={plan_summary.get('dry_run_only_count', 0)} "
                f"capped={plan_summary.get('capped_out_count', 0)} "
                f"risk={plan_summary.get('risk_blocked_count', 0)}"
            )
            lines.append(plan_line)
        if budget_summary is not None:
            budget_line = (
                f"Budget:        B={budget_summary.get('boost_count', 0)} "
                f"S={budget_summary.get('steady_count', 0)} "
                f"C={budget_summary.get('conserve_count', 0)} "
                f"H={budget_summary.get('hold_count', 0)} "
                f"F={budget_summary.get('freeze_count', 0)}"
            )
            if budget_runtime is not None:
                budget_line += f" alloc={float(budget_runtime.get('allocated_total_bnb', 0.0)):.4f}"
            lines.append(budget_line)
        if quarantine_summary is not None:
            quarantine_line = (
                f"Quarantine:    B={quarantine_summary.get('block_count', 0)} "
                f"D={quarantine_summary.get('dry_run_count', 0)} "
                f"C={quarantine_summary.get('cooldown_count', 0)}"
            )
            next_expiry = None
            if quarantine_runtime is not None:
                next_expiry = quarantine_runtime.get('earliest_expiry_at')
            if next_expiry:
                quarantine_line += f" next={next_expiry}"
            lines.append(quarantine_line)
        if rebalance_summary is not None:
            rebalance_line = (
                f"Rebalance:     A={rebalance_summary.get('accelerate_count', 0)} "
                f"M={rebalance_summary.get('maintain_count', 0)} "
                f"T={rebalance_summary.get('trim_count', 0)} "
                f"C={rebalance_summary.get('cooldown_count', 0)} "
                f"S={rebalance_summary.get('stop_count', 0)}"
            )
            if rebalance_runtime is not None:
                rebalance_line += f" alloc={float(rebalance_runtime.get('rebalance_total_budget_bnb', 0.0)):.4f}"
            lines.append(rebalance_line)
        if unwind_summary is not None:
            unwind_line = (
                f"Unwind:        C={unwind_summary.get('cancel_now_count', 0)} "
                f"R={unwind_summary.get('reduce_count', 0)} "
                f"V={unwind_summary.get('review_count', 0)} "
                f"K={unwind_summary.get('keep_count', 0)}"
            )
            if unwind_runtime is not None:
                unwind_line += (
                    f" sel={int(unwind_runtime.get('selected_count', 0))}"
                    f" rel={float(unwind_runtime.get('selected_release_bnb', 0.0)):.4f}"
                )
            lines.append(unwind_line)
        if batch_summary is not None:
            batch_line = (
                f"Batch:         cols={batch_summary.get('selected_count', 0)} "
                f"live={batch_summary.get('executed_live_count', 0)} "
                f"dry={batch_summary.get('executed_dry_run_count', 0)} "
                f"submitted={batch_summary.get('submitted_count', 0)} "
                f"ready_left={batch_summary.get('remaining_ready_count', 0)}"
            )
            lines.append(batch_line)
            pre_sync_status = batch_summary.get('pre_sync_status')
            post_sync_status = batch_summary.get('post_sync_status')
            if pre_sync_status or post_sync_status:
                sync_line = (
                    f"Sync:          pre={pre_sync_status or 'n/a'} "
                    f"post={post_sync_status or 'n/a'}"
                )
                top_collection = batch_summary.get('quarantine_top_collection') or batch_summary.get('feedback_top_collection')
                top_band = batch_summary.get('quarantine_top_band') or batch_summary.get('feedback_top_band')
                if top_collection:
                    sync_line += f" top={top_collection}"
                    if top_band:
                        sync_line += f"/{top_band}"
                lines.append(sync_line)
        if self.settings.buyer_wallet_address:
            try:
                pnl_report = WalletPnlAnalyzer(settings=self.settings, store=self.store).build_report(
                    wallet=self.settings.buyer_wallet_address,
                    reference_limit=self.settings.wallet_pnl_reference_event_limit,
                    collection_limit=3,
                    open_limit=3,
                    closed_limit=6,
                )
                pnl_summary = pnl_report.summary
                if pnl_summary.trade_count > 0 or pnl_summary.open_position_count > 0:
                    lines.extend(
                        [
                            '------------------',
                            f"Realized PnL:  {_format_money_breakdown(pnl_summary.realized_pnl_by_currency, signed=True)}",
                            f"Unrealized:    {_format_money_breakdown(pnl_summary.unrealized_pnl_by_currency, signed=True)}",
                            f"Inventory:     {pnl_summary.open_position_count} open | {pnl_summary.priced_open_position_count} priced | win={pnl_summary.win_rate:.1f}%" if pnl_summary.win_rate is not None else f"Inventory:     {pnl_summary.open_position_count} open | {pnl_summary.priced_open_position_count} priced",
                        ]
                    )
            except Exception as exc:
                logger.warning("Dashboard wallet PnL unavailable: %s", exc)
        lines.extend(
            [
                '------------------',
                f'Collections:   {tracked_count} tracked',
                f'Parasites:     {parasites_text} detected',
            ]
        )
        return '\n'.join(lines)

    def _pnl_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /pnl [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /pnl [limit]'
        report = WalletPnlAnalyzer(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            collection_limit=limit,
            open_limit=limit,
            closed_limit=max(limit, 1) * 2,
        )
        return format_wallet_pnl_text(report, collection_limit=limit, position_limit=limit)

    def _fills_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /fills [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /fills [limit]'
        report = ExecutionFillReconciler(settings=self.settings, store=self.store).reconcile(
            wallet=self.settings.buyer_wallet_address,
            reference_limit=self.settings.execution_fill_reference_event_limit,
            chain=self.settings.execution_chain,
            window_hours=self.settings.execution_fill_reconcile_window_hours,
            price_tolerance_pct=self.settings.execution_fill_price_tolerance_pct,
            pre_submit_slack_minutes=self.settings.execution_fill_pre_submit_slack_minutes,
            limit=max(limit, 1) * 3,
        )
        return format_execution_fill_text(report, limit=limit)

    def _risk_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /risk [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /risk [limit]'
        report = PortfolioRiskAnalyzer(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            chain=self.settings.execution_chain,
        )
        return format_portfolio_risk_text(report, limit=limit)

    def _pnl_guard_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /pnlguard [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /pnlguard [limit]'
        report = PnlGuardAnalyzer(settings=self.settings, store=self.store).build_report(
            wallet=self.settings.buyer_wallet_address,
            reference_limit=self.settings.wallet_pnl_reference_event_limit,
            chain=self.settings.execution_chain,
            window_hours=self.settings.pnl_guard_window_hours,
        )
        return format_pnl_guard_text(report, limit=limit)

    def _execution_health_text(self, args: list[str]) -> str:
        if len(args) > 1:
            return 'Usage: /exechealth [limit]'
        try:
            limit = int(args[0]) if args else 5
        except ValueError:
            return 'Usage: /exechealth [limit]'
        report = ExecutionHealthAnalyzer(settings=self.settings).build_report(
            chain=self.settings.execution_chain,
            window_hours=self.settings.execution_health_window_hours,
            event_limit=self.settings.execution_health_event_limit,
        )
        return format_execution_health_text(report, limit=limit)

    def _arm_live_command(self, args: list[str]) -> str:
        minutes = 15
        reason_parts = list(args)
        if reason_parts and reason_parts[0].isdigit():
            minutes = max(int(reason_parts[0]), 1)
            reason_parts = reason_parts[1:]
        reason = ' '.join(reason_parts).strip() or 'telegram_arm_live'
        state = PositionState(self.settings.execution_db_path)
        state.audit_integrity()
        payload = state.arm_live(minutes=minutes, actor='telegram', reason=reason)
        return (
            'live_armed\n'
            f"armed={payload['armed']}\n"
            f"minutes={minutes}\n"
            f"expires_at={payload['expires_at']}\n"
            f"reason={reason}"
        )

    def _disarm_live_command(self, args: list[str]) -> str:
        reason = ' '.join(args).strip() or 'telegram_disarm_live'
        state = PositionState(self.settings.execution_db_path)
        state.audit_integrity()
        payload = state.disarm_live(actor='telegram', reason=reason)
        return (
            'live_disarmed\n'
            f"armed={payload['armed']}\n"
            f"reason={reason}"
        )

    def _killswitch_command(self, args: list[str]) -> str:
        if args:
            return 'Usage: /killswitch'
        state = PositionState(self.settings.execution_db_path)
        state.audit_integrity()
        api = OKXAPIClient(settings=self.settings)
        active_offers = state.get_active_offers(chain=self.settings.execution_chain)

        exchange_lookup_failed = False
        exchange_lookup_error: str | None = None
        exchange_order_hashes: list[str] = []
        # Map order_hash → protocolData.parameters for on-chain cancel fallback
        exchange_order_params: dict[str, dict] = {}
        try:
            seen: set[str] = set()
            for row in api.get_my_offers(
                chain=self.settings.execution_chain,
                require_all_endpoints=True,
            ):
                order_hash = str(row.get('offerId') or row.get('orderHash') or row.get('id') or '').strip()
                if not order_hash or order_hash in seen:
                    continue
                seen.add(order_hash)
                exchange_order_hashes.append(order_hash)
                # Extract protocolData.parameters for on-chain cancel fallback
                proto = row.get('protocolData', {})
                if isinstance(proto, str):
                    try:
                        proto = __import__('json').loads(proto)
                    except Exception:
                        proto = {}
                params = proto.get('parameters') if isinstance(proto, dict) else None
                if params:
                    exchange_order_params[order_hash] = params
        except Exception as exc:
            exchange_lookup_failed = True
            exchange_lookup_error = str(exc)
            logger.warning("Kill switch exchange lookup failed: %s", exc)
            exchange_order_hashes = [
                offer.order_hash for offer in active_offers if not offer.order_hash.startswith("dryrun-")
            ]

        live_cancelled = 0
        local_cancelled = 0
        already_gone = 0
        failed: list[str] = []
        failed_order_hashes: list[str] = []
        successful_live_cancels: set[str] = set()
        for order_hash in exchange_order_hashes:
            try:
                ok = api.cancel_offer(
                    order_hash,
                    chain=self.settings.execution_chain,
                    order_params=exchange_order_params.get(order_hash),
                )
            except Exception as exc:
                failed_order_hashes.append(order_hash)
                failed.append(f'{order_hash}:{exc}')
                logger.warning("Kill switch cancel failed for %s: %s", order_hash, exc)
                continue
            if ok:
                live_cancelled += 1
                successful_live_cancels.add(order_hash)
            else:
                failed_order_hashes.append(order_hash)
                failed.append(f'{order_hash}:cancel_failed')

        failed_hash_set = set(failed_order_hashes)
        for offer in active_offers:
            if offer.order_hash.startswith("dryrun-"):
                if state.mark_offer_status(order_hash=offer.order_hash, status="cancelled"):
                    local_cancelled += 1
                continue
            if offer.order_hash in successful_live_cancels:
                state.mark_offer_status(order_hash=offer.order_hash, status="cancelled")
                continue
            if offer.order_hash in failed_hash_set:
                state.mark_offer_status(order_hash=offer.order_hash, status="killswitch_failed")
                continue
            if not exchange_lookup_failed:
                if state.mark_offer_status(order_hash=offer.order_hash, status="cancelled"):
                    already_gone += 1

        # Also mark any exchange-only hashes that failed but weren't in active_offers
        active_hash_set = {offer.order_hash for offer in active_offers}
        for order_hash in failed_order_hashes:
            if order_hash not in active_hash_set:
                state.mark_offer_status(order_hash=order_hash, status="killswitch_failed")
        state.disarm_live(actor='telegram_killswitch', reason='telegram_killswitch')
        state.set_force_dry_run(True, reason='telegram_killswitch')
        state.set_runtime_value('killswitch_activated_at', datetime.now(timezone.utc).isoformat())
        state.set_runtime_value('killswitch_source', 'telegram')
        state.record_submit_event(
            engine='runtime',
            action_type='KILLSWITCH',
            collection='*',
            chain=self.settings.execution_chain,
            price_bnb=None,
            status='killswitch',
            reason=(
                f'exchange_seen={len(exchange_order_hashes)};live_cancelled={live_cancelled};'
                f'local_cancelled={local_cancelled};already_gone={already_gone};failed={len(failed)};'
                f'exchange_lookup_failed={1 if exchange_lookup_failed else 0}'
            ),
        )
        state.log_action(
            action_type='KILLSWITCH',
            collection='*',
            chain=self.settings.execution_chain,
            order_hash=None,
            old_price_bnb=None,
            new_price_bnb=None,
            reason='CRITICAL: operator kill switch activated',
            executed=len(failed) == 0,
            error='; '.join(failed) if failed else None,
            payload={
                'exchange_seen': len(exchange_order_hashes),
                'live_cancelled': live_cancelled,
                'local_cancelled': local_cancelled,
                'already_gone': already_gone,
                'exchange_lookup_failed': exchange_lookup_failed,
                'exchange_lookup_error': exchange_lookup_error,
                'failed': failed,
            },
        )
        return (
            'killswitch_activated\n'
            'dry_run=true\n'
            f'active_offers_seen={len(active_offers)}\n'
            f'exchange_seen={len(exchange_order_hashes)}\n'
            f'live_cancelled={live_cancelled}\n'
            f'local_cancelled={local_cancelled}\n'
            f'already_gone={already_gone}\n'
            f'failed={len(failed)}\n'
            f'zombies={len(failed)} (marked killswitch_failed, need manual cancel)'
        )

    def _parasite_detected_count(self) -> int | None:
        if not self.settings.parasite_wallets:
            return None
        makers = [wallet.lower() for wallet in self.settings.parasite_wallets if wallet]
        if not makers:
            return None
        placeholders = ','.join('?' for _ in makers)
        try:
            with sqlite3.connect(self.settings.offers_db_path) as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM offers
                    WHERE market = ?
                      AND chain = ?
                      AND status = 'active'
                      AND maker IN ({placeholders})
                    """,
                    ['okx', self.settings.execution_chain.lower(), *makers],
                ).fetchone()
        except sqlite3.OperationalError as exc:
            logger.warning("Dashboard parasite count unavailable: %s", exc)
            return None
        return int(row[0] or 0) if row is not None else 0

    @staticmethod
    def _format_counterbid_task(task: CounterBidTask) -> str:
        lines = [
            'counterbid_scan',
            f'collection={task.collection}',
            f'chain={task.chain}',
            f'valid={task.valid}',
            f'parasite_offer_bnb={task.parasite_offer_bnb:.6f}',
            f'counter_price_bnb={task.counter_price_bnb:.6f}',
            f'reason={task.reason}',
        ]
        if task.parasite_maker:
            lines.append(f'parasite_maker={task.parasite_maker}')
        if task.error:
            lines.append(f'error={task.error}')
        return '\n'.join(lines)

    @staticmethod
    def _format_mass_offer_result(result: MassOfferRunResult) -> str:
        lines = [
            'mass_offer',
            f'campaign_id={result.campaign_id}',
            f'collection={result.collection}',
            f'chain={result.chain}',
            f'dry_run={result.dry_run}',
            f'scanned={result.scanned_count}',
            f'targets={result.target_count}',
            f'submitted={result.submitted_count}',
            f'dry_run_items={result.dry_run_count}',
            f'skipped={result.skipped_count}',
            f'failed={result.failed_count}',
        ]
        if result.blocked_reason:
            lines.append(f'blocked_reason={result.blocked_reason}')
        policy = result.applied_policy or {}
        if policy:
            lines.extend(
                [
                    f"policy_source={policy.get('source')}",
                    f"policy_effective_dry_run={policy.get('effective_dry_run')}",
                    f"policy_max_total={policy.get('max_total')}",
                    f"policy_delay={policy.get('delay_seconds')}",
                ]
            )
        for item in result.results[:5]:
            lines.append(
                f"- #{item.token_id} | {item.status} | rarity={item.rarity or 'n/a'} | listed={item.listed}"
            )
        return '\n'.join(lines)


    def _spreads_text(self, args: list[str]) -> str:
        min_pct = float(args[0]) if args else 3.0
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        rows = self.store.fetch_analysis_events(limit=5000)
        spreads = detect_spreads(rows, min_spread_pct=min_pct, top_n=limit)
        return format_spreads_text(spreads)

    def _rankings_text(self, args: list[str]) -> str:
        limit = int(args[0]) if args and args[0].isdigit() else 5
        rows = self.store.fetch_analysis_events(limit=5000)
        rankings = rank_collections(rows, min_spread_pct=3.0, top_n=limit)
        return format_rankings_text(rankings)

    def _send_alerts_command(self, args: list[str]) -> str:
        min_pct = float(args[0]) if args else 3.0
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        rows = self.store.fetch_analysis_events(limit=5000)
        spreads = detect_spreads(rows, min_spread_pct=min_pct, top_n=limit)
        rankings = rank_collections(rows, min_spread_pct=min_pct, top_n=limit)
        payload = send_analytics_report(self.settings, spreads=spreads, rankings=rankings)
        sent = payload.get('sent', {})
        return f"analytics_report sent telegram={sent.get('telegram')} webhook={sent.get('webhook')} spreads={payload.get('spread_count')} rankings={payload.get('ranking_count')}"

    def _run_command(self, args: list[str]) -> str:
        if not args:
            return 'Usage: /run <collection|all> [trades|listings]'
        target = args[0]
        source_mode = args[1] if len(args) > 1 and args[1] in {'trades', 'listings'} else 'trades'
        if target == 'all':
            results = self.runner.run_all_once()
            return f'Triggered all collections: runs={len(results)} new_events={sum(len(item.result.new_events) for item in results)}'
        result = self.runner.run_collection_once(target_name=target, source_mode=source_mode)
        return (
            f'Triggered {result.target_name}/{result.source_mode}: pages={result.result.pages_fetched} '
            f'new_events={len(result.result.new_events)} deliveries={sum(1 for item in result.result.deliveries if item.delivered)}'
        )

    def _reset_cursor_command(self, args: list[str]) -> str:
        if not args:
            return 'Usage: /resetcursor <collection> [trades|listings]'
        target = self.registry.get(args[0])
        if target is None:
            return 'Unknown collection'
        source_mode = args[1] if len(args) > 1 and args[1] in {'trades', 'listings'} else 'trades'
        namespace = f'{target.market}:{source_mode}:{target.name}'
        self.store.clear_namespace(namespace)
        return f'Cursor reset for {target.name}/{source_mode}'

    def _health_text(self) -> str:
        result = run_healthcheck(self.settings, self.store)
        execution = result.payload.get('execution', {})
        integrity = execution.get('integrity', {})
        control = result.payload.get('health_alerts', {})
        return (
            f'healthy={result.healthy} reason={result.reason} age_seconds={result.age_seconds}\n'
            f"execution_db={execution.get('db_exists', False)} "
            f"active_offers={execution.get('active_offer_count', 0)} "
            f"killswitch_failed={execution.get('killswitch_failed_count', 0)} "
            f"integrity_issues={integrity.get('issue_count', 0)} "
            f"alerts={self._health_alert_control_summary(control)}"
        )

    def _write_metrics_text(self) -> str:
        payload = write_runtime_metrics(self.settings, self.store, extra={'daemon_status': 'telegram_admin', 'last_command': 'writemetrics'})
        execution = payload.get('execution', {})
        integrity = execution.get('integrity', {})
        control = payload.get('health_alerts', {})
        return (
            f"metrics_written generated_at={payload['generated_at']} event_count={payload['event_count']} "
            f"execution_active_offers={execution.get('active_offer_count', 0)} "
            f"execution_integrity_issues={integrity.get('issue_count', 0)} "
            f"alerts={self._health_alert_control_summary(control)}"
        )

    def _alert_status_text(self, args: list[str]) -> str:
        if args:
            return 'Usage: /alertstatus'
        result = run_healthcheck(self.settings, self.store)
        control = get_health_alert_control(self.store).to_dict()
        lines = [
            'alert_status',
            f'healthy={result.healthy}',
            f'reason={result.reason}',
            f'alerts={self._health_alert_control_summary(control)}',
            f"snooze_until={control.get('snooze_until') or 'none'}",
            f"ack_reason={control.get('acknowledged_reason') or 'none'}",
        ]
        if control.get('snoozed_by'):
            lines.append(f"snoozed_by={control['snoozed_by']}")
        if control.get('acknowledged_by'):
            lines.append(f"ack_by={control['acknowledged_by']}")
        return '\n'.join(lines)

    def _alert_ack_command(self, args: list[str]) -> str:
        note = ' '.join(args).strip() or 'telegram_alert_ack'
        result = run_healthcheck(self.settings, self.store)
        if not is_alertable_health_result(result):
            return (
                'alert_ack\n'
                'acknowledged=False\n'
                f'healthy={result.healthy}\n'
                f'reason={result.reason}'
            )
        control = acknowledge_health_alert(self.store, reason=result.reason, actor='telegram', note=note).to_dict()
        return (
            'alert_ack\n'
            'acknowledged=True\n'
            f'health_reason={result.reason}\n'
            f'note={note}\n'
            f"alerts={self._health_alert_control_summary(control)}"
        )

    def _alert_snooze_command(self, args: list[str]) -> str:
        minutes = 60
        reason_parts = list(args)
        if reason_parts and reason_parts[0].isdigit():
            minutes = max(int(reason_parts[0]), 1)
            reason_parts = reason_parts[1:]
        reason = ' '.join(reason_parts).strip() or 'telegram_alert_snooze'
        control = snooze_health_alerts(self.store, minutes=minutes, actor='telegram', reason=reason).to_dict()
        return (
            'alert_snooze\n'
            'snoozed=True\n'
            f'minutes={minutes}\n'
            f'reason={reason}\n'
            f"snooze_until={control.get('snooze_until')}\n"
            f"alerts={self._health_alert_control_summary(control)}"
        )

    def _alert_reset_command(self, args: list[str]) -> str:
        if args:
            return 'Usage: /alertreset'
        control = reset_health_alert_control(self.store).to_dict()
        return (
            'alert_reset\n'
            'reset=True\n'
            f"alerts={self._health_alert_control_summary(control)}"
        )

    @staticmethod
    def _health_alert_control_summary(control: dict[str, object]) -> str:
        if bool(control.get('snoozed')):
            remaining = control.get('snooze_remaining_seconds')
            if isinstance(remaining, (int, float)):
                minutes = max(int((float(remaining) + 59) // 60), 0)
                return f'SNOOZED ({minutes}m left)'
            return 'SNOOZED'
        acknowledged_reason = control.get('acknowledged_reason')
        if acknowledged_reason:
            return f'ACK {acknowledged_reason}'
        return 'ACTIVE'

    def _profiles_text(self) -> str:
        runtime = self.settings.app_profile
        desired = get_desired_profile(self.store, runtime)
        available = list_profiles(self.settings.profiles_dir)
        return 'Profiles:\n' + '\n'.join(
            f"- {name}{' (runtime)' if name == runtime else ''}{' (desired)' if name == desired else ''}"
            for name in available
        )

    def _profile_text(self) -> str:
        runtime = self.settings.app_profile
        desired = get_desired_profile(self.store, runtime)
        return f'runtime_profile={runtime}\ndesired_profile={desired}\nprofiles_dir={self.settings.profiles_dir}'

    def _set_profile_command(self, args: list[str]) -> str:
        if not args:
            return 'Usage: /setprofile <dev|stage|prod>'
        profile = args[0].strip().lower()
        available = set(list_profiles(self.settings.profiles_dir))
        if profile not in available:
            return f"Unknown profile. Available: {', '.join(sorted(available))}"
        set_desired_profile(self.store, profile)
        return f'Desired profile set to {profile}. Restart the service to apply it.'

    def _backup_command(self, args: list[str]) -> str:
        label = args[0] if args else 'telegram'
        artifact = backup_database(self.settings.db_path, self.settings.backup_dir, label=label)
        return f'Backup created: {artifact.path.name} size={artifact.size_bytes}'

    def _backups_text(self, args: list[str]) -> str:
        limit = int(args[0]) if args and args[0].isdigit() else 5
        items = list_backups(self.settings.backup_dir, limit=limit)
        if not items:
            return 'No backups found'
        return 'Recent backups:\n' + '\n'.join(f'- {item.name}' for item in items)

    def _restore_command(self, args: list[str]) -> str:
        if not args:
            return 'Usage: /restore <filename>'
        backup_path = resolve_backup_path(self.settings.backup_dir, args[0])
        result = restore_database(self.settings.db_path, backup_path, self.settings.backup_dir, create_safety_backup=True)
        safety = result.safety_backup_path.name if result.safety_backup_path else 'none'
        return f'Restored {backup_path.name} to {result.restored_to.name}; safety_backup={safety}'

    def _sales_stats_text(self) -> str:
        try:
            from okx_nft_bot.sales_stream import SalesDatabase
            import os as _os
            db = SalesDatabase(_os.getenv('SALES_DB_PATH', './data/sales_stream.sqlite3'))
            s = db.get_stats()
            lines = ['📊 Sales Stream Stats']
            lines.append(f'Total sales: {s["total_sales"]}')
            for market, count in s.get('by_market', {}).items():
                lines.append(f'  {market.upper()}: {count}')
            lines.append(f'Parasite-involved: {s.get("parasite_involved", 0)}')
            for market, ts in s.get('latest_by_market', {}).items():
                lines.append(f'  Latest {market.upper()}: {ts}')
            return '\n'.join(lines)
        except Exception as exc:
            return f'Sales stats error: {exc}'

    def _parasite_sales_text(self, *, limit: int = 10) -> str:
        try:
            from okx_nft_bot.sales_stream import SalesDatabase
            import os as _os
            db = SalesDatabase(_os.getenv('SALES_DB_PATH', './data/sales_stream.sqlite3'))
            with db._connect() as conn:
                rows = conn.execute(
                    'SELECT collection_name, price, currency, chain, trade_type, ts '
                    'FROM sales WHERE is_parasite_buyer=1 OR is_parasite_seller=1 '
                    'ORDER BY ts DESC LIMIT ?', (limit,)
                ).fetchall()
            if not rows:
                return 'No parasite sales found'
            lines = [f'🔴 Parasite sales (last {limit}):']
            for r in rows:
                name, price, cur, chain, ttype, ts = r
                lines.append(f'  {name[:25]} | {price:.4f} {cur} | {chain} | {ttype} | {ts}')
            return '\n'.join(lines)
        except Exception as exc:
            return f'Parasite sales error: {exc}'

    def _parasite_status_text(self) -> str:
        """Return parasite hunter status for /parasite command."""
        if not self.parasite_hunter:
            return '🎯 ParasiteHunter: not initialized'
        try:
            status = self.parasite_hunter.get_status()
            mode = 'DRY RUN' if status['dry_run'] else 'LIVE'
            enabled = '✅' if status['enabled'] else '❌'
            lines = [
                f'🎯 ParasiteHunter v4 {enabled} ({mode})',
                f'Targets: {status["target_wallets"]} wallets',
                f'Scans: {status["total_scans"]}',
                f'Already winning: {status["already_winning"]}',
                f'Interval: {status["scan_interval"]}s',
                f'Undercut: {status["undercut_bps"]}bps',
                f'Chains: {", ".join(status["chains"])}',
            ]
            scan = status.get('last_scan')
            if scan:
                lines.append(f'Last scan: {scan["offers_found"]} offers, '
                             f'{scan["undercuts_placed"]} undercuts, '
                             f'{scan["duration_sec"]:.1f}s')
            return '\n'.join(lines)
        except Exception as exc:
            return f'ParasiteHunter status error: {exc}'

    def _parasite_scan_command(self) -> str:
        """Trigger immediate parasite scan via /parasitescan."""
        if not self.parasite_hunter:
            return '🎯 ParasiteHunter: not initialized'
        if not self.parasite_hunter.enabled:
            return '🎯 ParasiteHunter: disabled (set PARASITE_HUNTER_ENABLED=1)'
        try:
            report = self.parasite_hunter.scan_wallet()
            self.parasite_hunter.last_report = report
            self.parasite_hunter.total_scans += 1
            return (
                f'🎯 Scan complete in {report.scan_duration_sec:.1f}s\n'
                f'WL: {report.wl_offers_found} offers, {report.wl_undercuts_placed} undercuts\n'
                f'non-WL: {report.nonwl_offers_found} offers, {report.nonwl_undercuts_placed} undercuts\n'
                f'Already best: {report.wl_already_best}'
            )
        except Exception as exc:
            return f'Scan failed: {exc}'

    def _parasite_live_toggle(self, args: list[str]) -> str:
        """Toggle DRY_RUN/LIVE mode via /parasitelive on|off.

        Going LIVE now respects the execution governor: force_dry_run
        and killswitch_failed offers block the switch.
        """
        if not self.parasite_hunter:
            return '🎯 ParasiteHunter: not initialized'
        arg = (args[0].strip().lower() if args else "")
        if arg == 'on':
            self.parasite_hunter.dry_run = True
            return (
                '🎯 BLOCKED: ParasiteHunter live mode is deprecated and stays DRY-RUN.\n'
                'Use okx-nft-exec / counterbid / undercutter for guarded live execution.'
            )
        elif arg == 'off':
            self.parasite_hunter.dry_run = True
            return '🎯 ParasiteHunter: DRY RUN mode (safe)'
        else:
            mode = 'LIVE' if not self.parasite_hunter.dry_run else 'DRY RUN'
            force_dry = ''
            try:
                state = PositionState(self.settings.execution_db_path)
                if state.is_force_dry_run():
                    force_dry = '\nforce_dry_run=ON (governor override)'
            except Exception as exc:
                logger.warning("ParasiteHunter mode status lookup failed: %s", exc)
            return f'🎯 Current mode: {mode}{force_dry}\nUsage: /parasitelive on|off'
