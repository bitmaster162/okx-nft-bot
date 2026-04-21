from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from eth_account import Account

from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.wei_utils import to_wei
from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.mass_offer.scanner import (
    MassOfferCandidate,
    MassOfferFilters,
    MassOfferSkip,
    build_existing_offer_map,
    fetch_collection_nfts,
    select_mass_offer_targets,
)
from okx_nft_bot.mass_offer.tracker import MassOfferCampaign, MassOfferRecord, MassOfferTracker
from okx_nft_bot.providers.offers_okx import OKXOffersProvider
from okx_nft_bot.signing.seaport_signer import BSC_RPC, build_order_payload, build_per_item_offer, get_counter, sign_order
from okx_nft_bot.undercutter.state import PositionState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MassOfferItemResult:
    token_id: int
    status: str
    owner: str | None
    rarity: str | None
    listed: bool
    existing_offer_bnb: float | None
    offer_ref: str | None = None
    reason: str | None = None
    preview_payload: dict[str, Any] | None = None


@dataclass(slots=True)
class MassOfferRunResult:
    campaign_id: int
    collection: str
    chain: str
    price_bnb: float
    duration_hours: int
    delay_seconds: float
    dry_run: bool
    scanned_count: int
    target_count: int
    submitted_count: int
    dry_run_count: int
    skipped_count: int
    failed_count: int
    results: list[MassOfferItemResult]
    skipped: list[MassOfferSkip]

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "collection": self.collection,
            "chain": self.chain,
            "price_bnb": self.price_bnb,
            "duration_hours": self.duration_hours,
            "delay_seconds": self.delay_seconds,
            "dry_run": self.dry_run,
            "scanned_count": self.scanned_count,
            "target_count": self.target_count,
            "submitted_count": self.submitted_count,
            "dry_run_count": self.dry_run_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "results": [asdict(item) for item in self.results],
            "skipped": [asdict(item) for item in self.skipped],
        }


class MassOfferEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        tracker: MassOfferTracker | None = None,
        market_client: OKXMarketplaceClient | None = None,
        api_client: OKXAPIClient | None = None,
        offers_provider: OKXOffersProvider | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self.settings = settings
        self.tracker = tracker or MassOfferTracker(settings.execution_db_path)
        self.market_client = market_client or OKXMarketplaceClient(settings=settings)
        self.api_client = api_client or OKXAPIClient(settings=settings, market_client=self.market_client)
        self.offers_provider = offers_provider or OKXOffersProvider(client=self.market_client, settings=settings)
        self.state = PositionState(settings.execution_db_path)
        self.governor = ExecutionGovernor(settings=settings, state=self.state, api_client=self.api_client)
        self.sleep_fn = sleep_fn or time.sleep

    def run(
        self,
        *,
        collection: str,
        chain: str = "bsc",
        price_bnb: float | None = None,
        rarity_filter: list[str] | tuple[str, ...] | None = None,
        unlisted_only: bool = False,
        exclude_own: bool = True,
        max_existing_offer: float | None = None,
        min_token_id: int | None = None,
        max_token_id: int | None = None,
        max_total: int | None = None,
        duration_hours: int | None = None,
        delay_seconds: float | None = None,
        dry_run: bool | None = None,
        max_pages: int | None = None,
        rpc_url: str = BSC_RPC,
    ) -> MassOfferRunResult:
        resolved_chain = chain.lower()
        if resolved_chain != "bsc":
            raise ValueError(f"Only 'bsc' is supported in the execution track; got {resolved_chain!r}")

        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        resolved_duration_hours = int(duration_hours if duration_hours is not None else self.settings.mass_offer_duration_hours)
        resolved_delay_seconds = float(delay_seconds if delay_seconds is not None else self.settings.mass_offer_delay_seconds)
        resolved_max_total = int(max_total if max_total is not None else self.settings.mass_offer_max_total)
        resolved_rarity_filter = tuple(value.strip().upper() for value in (rarity_filter or []) if value and value.strip())
        requested_dry_run = self.settings.mass_offer_dry_run if dry_run is None else bool(dry_run)
        effective_dry_run = self.governor.effective_dry_run(requested_dry_run)

        filters = MassOfferFilters(
            rarity_filter=resolved_rarity_filter,
            unlisted_only=unlisted_only,
            exclude_own=exclude_own,
            max_existing_offer=max_existing_offer,
            min_token_id=min_token_id,
            max_token_id=max_token_id,
        )
        campaign_id = self.tracker.start_campaign(
            collection=collection,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            duration_hours=resolved_duration_hours,
            delay_seconds=resolved_delay_seconds,
            dry_run=effective_dry_run,
            rarity_filter=resolved_rarity_filter,
            unlisted_only=unlisted_only,
            exclude_own=exclude_own,
            max_existing_offer=max_existing_offer,
            min_token_id=min_token_id,
            max_token_id=max_token_id,
            max_total=resolved_max_total,
        )

        assets = fetch_collection_nfts(
            self.market_client,
            chain=resolved_chain,
            collection=collection,
            limit=self.settings.okx_page_limit,
            max_pages=max_pages,
        )
        highest_existing_offer = self._existing_offer_map(
            chain=resolved_chain,
            collection=collection,
            enabled=max_existing_offer is not None,
        )
        scan_result = select_mass_offer_targets(
            assets,
            filters=filters,
            own_wallet=self.settings.buyer_wallet_address,
            existing_offer_prices=highest_existing_offer,
        )

        selected_targets = list(scan_result.targets)
        skipped_items = list(scan_result.skipped)
        if len(selected_targets) > resolved_max_total:
            overflow = selected_targets[resolved_max_total:]
            selected_targets = selected_targets[:resolved_max_total]
            skipped_items.extend(
                [
                    MassOfferSkip(
                        token_id=item.token_id,
                        reason="max_total_reached",
                        rarity=item.rarity,
                        owner=item.owner,
                        listed=item.listed,
                        existing_offer_bnb=item.existing_offer_bnb,
                    )
                    for item in overflow
                ]
            )

        for item in skipped_items:
            if item.token_id is None:
                continue
            self.tracker.record_item(
                campaign_id=campaign_id,
                collection=collection,
                chain=resolved_chain,
                token_id=item.token_id,
                owner=item.owner,
                rarity=item.rarity,
                listed=bool(item.listed),
                existing_offer_bnb=item.existing_offer_bnb,
                price_bnb=resolved_price_bnb,
                status="skipped",
                reason=item.reason,
            )

        results: list[MassOfferItemResult] = []
        submitted_count = 0
        dry_run_count = 0
        failed_count = 0
        account = self._load_buyer_account()
        duration_seconds = max(int(resolved_duration_hours * 3600), 1)
        price_wei = to_wei(resolved_price_bnb)

        for index, target in enumerate(selected_targets):
            # Allocate Seaport counter atomically from governor on every iteration.
            # This serialises with any other engine on the same wallet+chain
            # (e.g. ParasiteHunter counter-bids) via SQLite BEGIN IMMEDIATE in
            # PositionState.allocate_seaport_counter. A local `counter += 1`
            # would race if two processes are submitting concurrently.
            counter = self.governor.allocate_seaport_counter(
                account.address, resolved_chain
            )
            result = self._submit_target(
                campaign_id=campaign_id,
                collection=collection,
                chain=resolved_chain,
                target=target,
                offerer=account.address,
                private_key=self.settings.buyer_wallet_private_key or "",
                price_bnb=resolved_price_bnb,
                price_wei=price_wei,
                counter=counter,
                duration_seconds=duration_seconds,
                dry_run=effective_dry_run,
            )
            results.append(result)
            if result.status == "active":
                submitted_count += 1
            elif result.status == "dry_run":
                dry_run_count += 1
            elif result.status == "failed":
                failed_count += 1
            elif result.status == "blocked":
                failed_count += 1

            if not effective_dry_run and index < len(selected_targets) - 1 and resolved_delay_seconds > 0:
                self.sleep_fn(resolved_delay_seconds)

        self.tracker.complete_campaign(
            campaign_id=campaign_id,
            status="completed",
            scanned_count=len(assets),
            target_count=len(selected_targets),
            submitted_count=submitted_count,
            dry_run_count=dry_run_count,
            skipped_count=len(skipped_items),
            failed_count=failed_count,
        )
        return MassOfferRunResult(
            campaign_id=campaign_id,
            collection=collection.lower(),
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            duration_hours=resolved_duration_hours,
            delay_seconds=resolved_delay_seconds,
            dry_run=effective_dry_run,
            scanned_count=len(assets),
            target_count=len(selected_targets),
            submitted_count=submitted_count,
            dry_run_count=dry_run_count,
            skipped_count=len(skipped_items),
            failed_count=failed_count,
            results=results,
            skipped=skipped_items,
        )

    def place_single_offer(
        self,
        *,
        collection_address: str,
        token_id: str | int,
        price_wbnb: float,
        currency_address: str | None = None,
        chain: str = "bsc",
        duration_hours: int | None = None,
        dry_run: bool | None = None,
        quantity: int = 1,
        price_bnb_for_cap: float | None = None,
    ) -> bool:
        """Place a single token-level offer.  Used by ParasiteHunter for BSC undercuts.

        Args:
            collection_address: NFT collection contract address.
            token_id: Token ID (str or int).
            price_wbnb: Offer price in native units (e.g. BNB for WBNB, or amount for USDT).
            currency_address: ERC-20 token address to bid with.
                              Defaults to WBNB if not provided.
            chain: Blockchain (only 'bsc' supported for now).
            duration_hours: Offer duration; defaults to settings.mass_offer_duration_hours.
            dry_run: Override dry-run flag; defaults to settings.mass_offer_dry_run.
            quantity: Number of items to buy in this offer (default 1).
            price_bnb_for_cap: Optional BNB-equivalent of the offer for governor
                daily cap check. Required when currency is not WBNB/BNB (e.g.
                USDT/USDC), otherwise the cap (max_bnb_per_day) treats the raw
                token amount as BNB and incorrectly blocks legitimate offers.
                If None, falls back to price_wbnb.

        Returns:
            True if the offer was submitted (or dry-run recorded) successfully.
        """
        resolved_chain = chain.lower()
        resolved_duration = int(
            duration_hours if duration_hours is not None else self.settings.mass_offer_duration_hours
        )
        requested_dry_run = self.settings.mass_offer_dry_run if dry_run is None else bool(dry_run)
        effective_dry_run = self.governor.effective_dry_run(requested_dry_run)
        duration_seconds = max(int(resolved_duration * 3600), 1)

        from okx_nft_bot.signing.seaport_signer import WBNB_ADDRESS

        price_wei = to_wei(price_wbnb)
        account = self._load_buyer_account()

        # Determine if this is a token-level or collection-level offer
        is_collection_offer = not token_id or str(token_id) in ("", "0", "col", "collection")
        int_token_id = 0 if is_collection_offer else int(token_id)

        now = int(time.time())
        valid_time = now + duration_seconds
        resolved_currency = currency_address or WBNB_ADDRESS

        if effective_dry_run:
            offer_ref = f"dryrun:single:{int_token_id}"
            logger.info(
                "place_single_offer DRY-RUN %s token=%s price=%.6f",
                collection_address[:14],
                int_token_id,
                price_wbnb,
            )
            self.state.upsert_active_offer(
                order_hash=offer_ref,
                collection=collection_address,
                chain=resolved_chain,
                price_bnb=price_wbnb,
                status="dry_run",
                current_floor=price_wbnb,
            )
            return True

        # Use BNB-equivalent for daily cap check when currency is not WBNB.
        # Falls back to price_wbnb for backward compatibility (mass_offer campaigns).
        cap_check_price = price_bnb_for_cap if price_bnb_for_cap is not None else price_wbnb
        blocked_reason = self.governor.check_live_submit_allowed(
            action_type="LIVE_SINGLE_OFFER",
            collection=collection_address,
            chain=resolved_chain,
            price_bnb=cap_check_price,
            configured_dry_run=False,
        )
        if blocked_reason:
            logger.warning(
                "place_single_offer BLOCKED %s token=%s: %s",
                collection_address[:14],
                int_token_id,
                blocked_reason,
            )
            return False

        # Use OKX high-level create-offer API (same pattern as create-listing)
        try:
            result = self.api_client.create_offer(
                chain=resolved_chain,
                wallet_address=account.address,
                collection_address=collection_address,
                token_id=int_token_id if not is_collection_offer else "",
                price_raw=str(price_wei),
                currency_address=resolved_currency,
                valid_time=valid_time,
                count=max(1, quantity),
            )
        except Exception as exc:
            logger.error(
                "place_single_offer FAILED %s token=%s price=%.6f: %s",
                collection_address[:14], int_token_id, price_wbnb, exc,
            )
            return False

        offer_id = result.get("offer_id")
        if offer_id:
            self.state.upsert_active_offer(
                order_hash=str(offer_id),
                collection=collection_address,
                chain=resolved_chain,
                price_bnb=price_wbnb,
                status="active",
                current_floor=price_wbnb,
            )
            logger.info(
                "place_single_offer SUCCESS %s token=%s price=%.6f offer=%s",
                collection_address[:14], int_token_id, price_wbnb, offer_id,
            )
            return True
        else:
            logger.warning(
                "place_single_offer FAILED %s token=%s price=%.6f — no offer_id in response",
                collection_address[:14], int_token_id, price_wbnb,
            )
            return False

    def status(self, *, chain: str = "bsc", limit: int = 5) -> dict[str, Any]:
        resolved_chain = chain.lower()
        integrity = self.state.audit_integrity().to_dict()
        campaigns = self.tracker.list_campaigns(limit=limit)
        active = self._list_synced_active_records(chain=resolved_chain)
        rate_limits = self.governor.get_rate_limit_snapshot(chain=resolved_chain, now=datetime.now(timezone.utc))
        return {
            "chain": resolved_chain,
            "global_dry_run": self.settings.dry_run,
            "mass_offer_dry_run": self.settings.mass_offer_dry_run,
            "forced_dry_run": self.state.is_force_dry_run(),
            "effective_dry_run": self.governor.effective_dry_run(self.settings.mass_offer_dry_run),
            "live_arm": self.governor.get_live_arm_state(now=datetime.now(timezone.utc)),
            "integrity": integrity,
            "active_offer_count": len(active),
            "active_offers": [self._record_to_dict(item) for item in active],
            "campaigns": [self._campaign_to_dict(item) for item in campaigns],
            "rate_limits": {
                **rate_limits,
                "last_submit_at": rate_limits["last_submit_at"].isoformat() if rate_limits["last_submit_at"] else None,
            },
        }

    def cancel_active(self, *, chain: str = "bsc", collection: str | None = None) -> dict[str, Any]:
        resolved_chain = chain.lower()
        active = self._list_synced_active_records(chain=resolved_chain, collection=collection)
        cancelled = 0
        failed: list[str] = []
        touched_campaigns: set[int] = set()
        for item in active:
            try:
                order_params = self._fetch_order_params(item.offer_ref, resolved_chain) if item.offer_ref else None
                ok = bool(item.offer_ref) and self.api_client.cancel_offer(
                    item.offer_ref,
                    chain=resolved_chain,
                    order_params=order_params,
                )
            except Exception as exc:
                failed.append(f"{item.token_id}:{exc}")
                continue
            if ok:
                cancelled += 1
                touched_campaigns.add(item.campaign_id)
                self.tracker.mark_item_status(record_id=item.record_id, status="cancelled", reason="mass_offer_cancel")
                if item.offer_ref:
                    self.state.mark_offer_status(order_hash=item.offer_ref, status="cancelled")
            else:
                failed.append(f"{item.token_id}:cancel_failed")

        for campaign_id in touched_campaigns:
            self.tracker.mark_campaign_status(campaign_id=campaign_id, status="cancelled")

        return {
            "chain": resolved_chain,
            "active_seen": len(active),
            "cancelled": cancelled,
            "failed": failed,
            "collection": collection.lower() if collection else None,
        }

    def _fetch_order_params(self, order_hash: str | None, chain: str) -> dict[str, Any] | None:
        if not order_hash:
            return None
        try:
            for row in self.api_client.get_my_offers(chain=chain, require_all_endpoints=True):
                candidate = str(row.get("offerId") or row.get("orderHash") or row.get("id") or "").strip()
                if candidate != order_hash:
                    continue
                proto = row.get("protocolData", {})
                if isinstance(proto, str):
                    return None
                params = proto.get("parameters") if isinstance(proto, dict) else None
                if isinstance(params, dict):
                    return params
                return None
        except Exception as exc:
            logger.warning(
                "mass_offer cancel: failed to fetch order params for %s on %s: %s",
                order_hash[:14],
                chain,
                exc,
            )
        return None

    def _list_synced_active_records(
        self,
        *,
        chain: str,
        collection: str | None = None,
    ) -> list[MassOfferRecord]:
        active = self.tracker.list_active_records(chain=chain, collection=collection)
        runtime = self.state.get_runtime_state()
        if "last_reconcile_at" not in runtime:
            return active
        active_hashes = {offer.order_hash for offer in self.state.get_active_offers(chain=chain)}
        synced: list[MassOfferRecord] = []
        for item in active:
            if item.offer_ref and item.offer_ref in active_hashes:
                synced.append(item)
                continue
            self.tracker.mark_item_status(
                record_id=item.record_id,
                status="exchange_missing",
                reason="state_reconciled_missing",
            )
        return synced

    def _submit_target(
        self,
        *,
        campaign_id: int,
        collection: str,
        chain: str,
        target: MassOfferCandidate,
        offerer: str,
        private_key: str,
        price_bnb: float,
        price_wei: int,
        counter: int,
        duration_seconds: int,
        dry_run: bool,
    ) -> MassOfferItemResult:
        from okx_nft_bot.signing.seaport_signer import WBNB_ADDRESS

        preview_payload: dict[str, Any] | None = None
        try:
            now = int(time.time())
            valid_time = now + duration_seconds
            preview_payload = {
                "dry_run": dry_run,
                "chain": chain,
                "offerer": offerer,
                "collection": collection.lower(),
                "token_id": target.token_id,
                "price_bnb": price_bnb,
                "price_wei": price_wei,
                "counter": counter,
                "valid_time": valid_time,
            }
            if dry_run:
                offer_ref = f"dryrun:{campaign_id}:{target.token_id}"
                self.tracker.record_item(
                    campaign_id=campaign_id,
                    collection=collection,
                    chain=chain,
                    token_id=target.token_id,
                    owner=target.owner,
                    rarity=target.rarity,
                    listed=target.listed,
                    existing_offer_bnb=target.existing_offer_bnb,
                    price_bnb=price_bnb,
                    status="dry_run",
                    reason="dry_run",
                    offer_ref=offer_ref,
                    payload=preview_payload,
                )
                return MassOfferItemResult(
                    token_id=target.token_id,
                    status="dry_run",
                    owner=target.owner,
                    rarity=target.rarity,
                    listed=target.listed,
                    existing_offer_bnb=target.existing_offer_bnb,
                    offer_ref=offer_ref,
                    reason="dry_run",
                    preview_payload=preview_payload,
                )

            blocked_reason = self.governor.check_live_submit_allowed(
                action_type="LIVE_MASS_OFFER",
                collection=collection,
                chain=chain,
                price_bnb=price_bnb,
                configured_dry_run=False,
            )
            if blocked_reason:
                self.tracker.record_item(
                    campaign_id=campaign_id,
                    collection=collection,
                    chain=chain,
                    token_id=target.token_id,
                    owner=target.owner,
                    rarity=target.rarity,
                    listed=target.listed,
                    existing_offer_bnb=target.existing_offer_bnb,
                    price_bnb=price_bnb,
                    status="blocked",
                    reason=blocked_reason,
                    payload=preview_payload,
                )
                self.state.record_submit_event(
                    engine="mass_offer",
                    action_type="LIVE_MASS_OFFER_BLOCKED",
                    collection=collection,
                    chain=chain,
                    price_bnb=price_bnb,
                    status="blocked",
                    reason=blocked_reason,
                )
                return MassOfferItemResult(
                    token_id=target.token_id,
                    status="blocked",
                    owner=target.owner,
                    rarity=target.rarity,
                    listed=target.listed,
                    existing_offer_bnb=target.existing_offer_bnb,
                    reason=blocked_reason,
                    preview_payload=preview_payload,
                )

            submit_result = self.api_client.create_offer(
                chain=chain,
                wallet_address=offerer,
                collection_address=collection,
                token_id=str(target.token_id),
                price_raw=str(price_wei),
                currency_address=WBNB_ADDRESS,
                valid_time=valid_time,
            )
            offer_ref = str(submit_result.get("offer_id") or submit_result.get("status") or f"submitted:{target.token_id}")
            self.tracker.record_item(
                campaign_id=campaign_id,
                collection=collection,
                chain=chain,
                token_id=target.token_id,
                owner=target.owner,
                rarity=target.rarity,
                listed=target.listed,
                existing_offer_bnb=target.existing_offer_bnb,
                price_bnb=price_bnb,
                status="active",
                reason=str(submit_result.get("status") or "submitted"),
                offer_ref=offer_ref,
                payload=preview_payload,
            )
            self.state.upsert_active_offer(
                order_hash=offer_ref,
                collection=collection,
                chain=chain,
                price_bnb=price_bnb,
                status="active",
                current_floor=price_bnb,
                preview_payload=preview_payload,
            )
            self.state.record_submit_event(
                engine="mass_offer",
                action_type="LIVE_MASS_OFFER",
                collection=collection,
                chain=chain,
                price_bnb=price_bnb,
                status="submitted",
                reason=f"offer_id={offer_ref};token_id={target.token_id}",
            )
            return MassOfferItemResult(
                token_id=target.token_id,
                status="active",
                owner=target.owner,
                rarity=target.rarity,
                listed=target.listed,
                existing_offer_bnb=target.existing_offer_bnb,
                offer_ref=offer_ref,
                reason=str(submit_result.get("status") or "submitted"),
                preview_payload=preview_payload,
            )
        except Exception as exc:
            self.tracker.record_item(
                campaign_id=campaign_id,
                collection=collection,
                chain=chain,
                token_id=target.token_id,
                owner=target.owner,
                rarity=target.rarity,
                listed=target.listed,
                existing_offer_bnb=target.existing_offer_bnb,
                price_bnb=price_bnb,
                status="failed",
                reason=str(exc),
                payload=preview_payload,
            )
            if not dry_run:
                self.state.record_submit_event(
                    engine="mass_offer",
                    action_type="LIVE_MASS_OFFER",
                    collection=collection,
                    chain=chain,
                    price_bnb=price_bnb,
                    status="failed",
                    reason=str(exc),
                )
            return MassOfferItemResult(
                token_id=target.token_id,
                status="failed",
                owner=target.owner,
                rarity=target.rarity,
                listed=target.listed,
                existing_offer_bnb=target.existing_offer_bnb,
                reason=str(exc),
                preview_payload=preview_payload,
            )

    def _existing_offer_map(self, *, chain: str, collection: str, enabled: bool) -> dict[str, float]:
        if not enabled:
            return {}
        offers = self.offers_provider.fetch_all_pages(
            chain=chain,
            collection_address=collection,
            max_pages=self.settings.offers_max_pages_per_run,
        )
        return build_existing_offer_map(offers)

    def _load_buyer_account(self) -> Any:
        if not self.settings.buyer_wallet_private_key or not self.settings.buyer_wallet_address:
            raise RuntimeError("BUYER_WALLET_ADDRESS and BUYER_WALLET_PRIVATE_KEY are required")
        account = Account.from_key(self.settings.buyer_wallet_private_key)
        if account.address.lower() != self.settings.buyer_wallet_address.lower():
            raise RuntimeError("BUYER_WALLET_ADDRESS does not match BUYER_WALLET_PRIVATE_KEY")
        return account

    @staticmethod
    def _campaign_to_dict(campaign: MassOfferCampaign) -> dict[str, Any]:
        return {
            "campaign_id": campaign.campaign_id,
            "collection": campaign.collection,
            "chain": campaign.chain,
            "price_bnb": campaign.price_bnb,
            "duration_hours": campaign.duration_hours,
            "delay_seconds": campaign.delay_seconds,
            "dry_run": campaign.dry_run,
            "status": campaign.status,
            "rarity_filter": list(campaign.rarity_filter),
            "unlisted_only": campaign.unlisted_only,
            "exclude_own": campaign.exclude_own,
            "max_existing_offer": campaign.max_existing_offer,
            "min_token_id": campaign.min_token_id,
            "max_token_id": campaign.max_token_id,
            "max_total": campaign.max_total,
            "scanned_count": campaign.scanned_count,
            "target_count": campaign.target_count,
            "submitted_count": campaign.submitted_count,
            "dry_run_count": campaign.dry_run_count,
            "skipped_count": campaign.skipped_count,
            "failed_count": campaign.failed_count,
            "created_at": campaign.created_at.isoformat(),
            "updated_at": campaign.updated_at.isoformat(),
        }

    @staticmethod
    def _record_to_dict(record: MassOfferRecord) -> dict[str, Any]:
        return {
            "record_id": record.record_id,
            "campaign_id": record.campaign_id,
            "collection": record.collection,
            "chain": record.chain,
            "token_id": record.token_id,
            "owner": record.owner,
            "rarity": record.rarity,
            "listed": record.listed,
            "existing_offer_bnb": record.existing_offer_bnb,
            "price_bnb": record.price_bnb,
            "status": record.status,
            "reason": record.reason,
            "offer_ref": record.offer_ref,
            "created_at": record.created_at,
        }
