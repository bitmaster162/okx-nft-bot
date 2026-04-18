from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import sqlite3
from datetime import datetime, timezone
from urllib import parse

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
from okx_nft_bot.mass_offer import MassOfferEngine, MassOfferRunResult
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
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        registry: CollectionRegistry,
        runner: MultiCollectionRunner,
        client: TelegramBotClient,
        parasite_hunter_loader: Callable[[], object] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.runner = runner
        self.client = client
        self.namespace = 'telegram_bot'
        self.parasite_hunter = None  # set externally by sales_stream or cli
        self._parasite_hunter_loader = parasite_hunter_loader

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

    def _is_blocked(self, chat_id: str) -> bool:
        allowed = self.settings.telegram_admin_chat_ids
        # SECURITY: if no admin IDs configured, block ALL — fail-closed
        if not allowed:
            return True
        return chat_id not in allowed

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
        if command == '/massoffercancel':
            return self._mass_offer_cancel_command(args)
        if command == '/undercutstatus':
            return self._undercut_status_text()
        if command == '/dashboard':
            return self._dashboard_command(args)
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

    def _get_parasite_hunter(self):
        if self.parasite_hunter is not None:
            return self.parasite_hunter
        if self._parasite_hunter_loader is None:
            return None
        try:
            self.parasite_hunter = self._parasite_hunter_loader()
        except Exception as exc:
            logger.warning("ParasiteHunter lazy init failed: %s", exc)
            self._parasite_hunter_loader = None
            return None
        self._parasite_hunter_loader = None
        return self.parasite_hunter

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
            '/massoffercancel - cancel active per-item mass offers\n'
            '/undercutstatus - show dry-run undercutter status\n'
            '/dashboard - show execution dashboard summary\n'
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
        # Also count actual live submits from execution_submit_log
        # (undercut_log may undercount if counterbid engine doesn't log there)
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
        if arm_state['armed']:
            arm_text = f"yes ({arm_state['minutes_remaining']}m left)"
        else:
            arm_text = 'no'
        integrity_text = 'OK' if integrity['ok'] else (
            f"issues={integrity['issue_count']}, quarantined={integrity['quarantine_count']}"
        )
        return (
            'NFT Bot Dashboard\n'
            '------------------\n'
            f'Mode:          {mode}\n'
            f'Live Armed:    {arm_text}\n'
            f'Integrity:     {integrity_text}\n'
            f'Active Offers: {active_count}\n'
            '------------------\n'
            f"Today Actions: {summary['total']} ({summary['attacks']} attacks, {summary['withdraws']} withdraws)"
            f"{f' [submits: {submit_count}]' if submit_count is not None else ''}\n"
            f"BNB Spent:     {summary['bnb_spent']:.4f} / {self.settings.max_bnb_per_day:.4f} limit\n"
            f'Rate:          {hourly} / {self.settings.max_live_offers_per_hour} per hour\n'
            f'Reconciled:    {reconcile_at}\n'
            '------------------\n'
            f'Collections:   {tracked_count} tracked\n'
            f'Parasites:     {parasites_text} detected'
        )

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
        hunter = self._get_parasite_hunter()
        if not hunter:
            return '🎯 ParasiteHunter: not initialized'
        try:
            status = hunter.get_status()
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
            if status.get('live_mode_deprecated'):
                lines.append('Legacy live path disabled; use okx-nft-exec for guarded execution')
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
        hunter = self._get_parasite_hunter()
        if not hunter:
            return '🎯 ParasiteHunter: not initialized'
        if not hunter.enabled:
            return '🎯 ParasiteHunter: disabled (set PARASITE_HUNTER_ENABLED=1)'
        try:
            report = hunter.scan_wallet()
            hunter.last_report = report
            hunter.total_scans += 1
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
        hunter = self._get_parasite_hunter()
        if not hunter:
            return '🎯 ParasiteHunter: not initialized'
        arg = (args[0].strip().lower() if args else "")
        if arg == 'on':
            hunter.dry_run = True
            return (
                '🎯 BLOCKED: ParasiteHunter live mode is deprecated and stays DRY-RUN.\n'
                'Use okx-nft-exec / counterbid / undercutter for guarded live execution.'
            )
        elif arg == 'off':
            hunter.dry_run = True
            return '🎯 ParasiteHunter: DRY RUN mode (safe)'
        else:
            mode = 'LIVE' if not hunter.dry_run else 'DRY RUN'
            force_dry = ''
            try:
                state = PositionState(self.settings.execution_db_path)
                if state.is_force_dry_run():
                    force_dry = '\nforce_dry_run=ON (governor override)'
            except Exception as exc:
                logger.warning("ParasiteHunter mode status lookup failed: %s", exc)
            return f'🎯 Current mode: {mode}{force_dry}\nUsage: /parasitelive on|off'
   