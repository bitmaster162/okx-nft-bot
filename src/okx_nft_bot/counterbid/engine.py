from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.config import CollectionConfig, CounterbidConfigManager
from okx_nft_bot.counterbid.okx_api import OKXAPIClient, OfferRefreshResult
from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.notifiers.base import AlertEnvelope
from okx_nft_bot.notifiers.telegram import TelegramNotifier
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.signing import preview_counterbid
from okx_nft_bot.undercutter.state import PositionState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CounterBidTask:
    collection: str
    chain: str
    parasite_offer_bnb: float
    counter_price_bnb: float
    reason: str
    valid: bool
    error: str | None = None
    parasite_maker: str | None = None
    preview_payload: dict[str, Any] | None = None
    action_type: str = "SCAN"
    submit_result: dict[str, Any] | None = None


@dataclass(slots=True)
class BatchResult:
    chain: str
    tasks: list[CounterBidTask]
    refresh_results: list[OfferRefreshResult] = field(default_factory=list)

    @property
    def valid_count(self) -> int:
        return sum(1 for task in self.tasks if task.valid)

    @property
    def invalid_count(self) -> int:
        return sum(1 for task in self.tasks if not task.valid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "tasks": [asdict(task) for task in self.tasks],
            "refresh_results": [asdict(item) for item in self.refresh_results],
        }


class CounterBidder:
    def __init__(
        self,
        *,
        settings: Settings,
        config_manager: CounterbidConfigManager | None = None,
        okx_client: OKXAPIClient | None = None,
    ) -> None:
        self.settings = settings
        self.config = config_manager or CounterbidConfigManager(settings.execution_db_path)
        self.okx = okx_client or OKXAPIClient(settings=settings)
        self.state = PositionState(settings.execution_db_path)
        self.governor = ExecutionGovernor(settings=settings, state=self.state, api_client=self.okx)
        self.parasite_wallets = {wallet.lower() for wallet in settings.parasite_wallets}

    def detect_parasite_offers(self, offers: list[NormalizedOffer]) -> NormalizedOffer | None:
        parasite_offers = [offer for offer in offers if (offer.maker or "").lower() in self.parasite_wallets]
        if not parasite_offers:
            return None
        return max(parasite_offers, key=lambda offer: float(offer.price or 0.0))

    def calculate_counter_price(
        self,
        collection_config: CollectionConfig,
        parasite_offer_bnb: float,
    ) -> tuple[float, str]:
        if parasite_offer_bnb > 0:
            proposed = parasite_offer_bnb + collection_config.margin_bnb
            reason = f"Parasite {parasite_offer_bnb:.6f} + margin {collection_config.margin_bnb:.6f}"
        else:
            proposed = collection_config.min_price_bnb
            reason = f"No parasite; min price {collection_config.min_price_bnb:.6f}"

        if proposed < collection_config.min_price_bnb:
            proposed = collection_config.min_price_bnb
            reason += f" -> clamped to min {collection_config.min_price_bnb:.6f}"
        if proposed > collection_config.max_price_bnb:
            proposed = collection_config.max_price_bnb
            reason += f" -> clamped to max {collection_config.max_price_bnb:.6f}"
        return proposed, reason

    def validate_counter_bid(
        self,
        collection_config: CollectionConfig,
        counter_price_bnb: float,
        parasite_offer_bnb: float,
    ) -> tuple[bool, str | None]:
        if counter_price_bnb < collection_config.min_price_bnb:
            return False, f"Counter price {counter_price_bnb:.6f} < min {collection_config.min_price_bnb:.6f}"
        if counter_price_bnb > collection_config.max_price_bnb:
            return False, f"Counter price {counter_price_bnb:.6f} > max {collection_config.max_price_bnb:.6f}"
        if parasite_offer_bnb > 0 and counter_price_bnb <= parasite_offer_bnb:
            return False, f"Counter {counter_price_bnb:.6f} not above parasite {parasite_offer_bnb:.6f}"
        return True, None

    def build_signed_counter_bid(self, *, collection: str, price_bnb: float, chain: str) -> dict[str, Any]:
        return preview_counterbid(
            settings=self.settings,
            collection=collection,
            price_bnb=price_bnb,
            chain=chain,
        )

    def process_single_collection(
        self,
        collection_address: str,
        *,
        chain: str | None = None,
        refresh: bool = False,
        sign_preview: bool = False,
        limit: int = 100,
    ) -> tuple[CounterBidTask, OfferRefreshResult | None]:
        resolved_chain = (chain or self.settings.execution_chain).lower()
        if resolved_chain != "bsc":
            raise ValueError(f"Only 'bsc' is supported in the execution track; got {resolved_chain!r}")
        cfg = self.config.get_collection(collection_address)
        if not cfg:
            return (
                CounterBidTask(
                    collection=collection_address,
                    chain=resolved_chain,
                    parasite_offer_bnb=0.0,
                    counter_price_bnb=0.0,
                    reason="No config",
                    valid=False,
                    error=f"Collection {collection_address} not found in config",
                ),
                None,
            )
        if not cfg.enabled:
            return (
                CounterBidTask(
                    collection=cfg.address,
                    chain=resolved_chain,
                    parasite_offer_bnb=0.0,
                    counter_price_bnb=0.0,
                    reason="Disabled",
                    valid=False,
                    error=f"Collection {cfg.address} is disabled",
                ),
                None,
            )

        refresh_result: OfferRefreshResult | None = None
        try:
            if refresh:
                refresh_result = self.okx.refresh_offers(
                    collection=cfg.address,
                    chain=resolved_chain,
                    max_pages=self.settings.offers_max_pages_per_run,
                )
            offers = self.okx.fetch_offers(
                collection=cfg.address,
                chain=resolved_chain,
                limit=limit,
                refresh=False,
            )
        except Exception as exc:
            return (
                CounterBidTask(
                    collection=cfg.address,
                    chain=resolved_chain,
                    parasite_offer_bnb=0.0,
                    counter_price_bnb=0.0,
                    reason="Fetch failed",
                    valid=False,
                    error=str(exc),
                ),
                refresh_result,
            )

        parasite_offer = self.detect_parasite_offers(offers)
        parasite_price_bnb = float(parasite_offer.price or 0.0) if parasite_offer else 0.0
        counter_price_bnb, reason = self.calculate_counter_price(cfg, parasite_price_bnb)
        is_valid, error = self.validate_counter_bid(cfg, counter_price_bnb, parasite_price_bnb)

        preview_payload: dict[str, Any] | None = None
        submit_result: dict[str, Any] | None = None
        action_type = "SCAN"
        effective_dry_run = self.governor.effective_dry_run()
        if is_valid and sign_preview and self.settings.buyer_wallet_address and self.settings.buyer_wallet_private_key:
            try:
                preview_payload = self.build_signed_counter_bid(
                    collection=cfg.address,
                    price_bnb=counter_price_bnb,
                    chain=resolved_chain,
                )
                if effective_dry_run:
                    action_type = "DRY_COUNTERBID"
                    self.state.upsert_active_offer(
                        order_hash=self._dry_order_hash(
                            collection=cfg.address,
                            price_bnb=counter_price_bnb,
                            preview_payload=preview_payload,
                        ),
                        collection=cfg.address,
                        chain=resolved_chain,
                        price_bnb=counter_price_bnb,
                        status="active",
                        current_floor=parasite_price_bnb if parasite_price_bnb > 0 else None,
                        preview_payload=preview_payload,
                    )
                else:
                    from okx_nft_bot.prices import to_usd

                    blocked_reason = self.governor.check_live_submit_allowed(
                        action_type="LIVE_COUNTERBID",
                        collection=cfg.address,
                        chain=resolved_chain,
                        price_bnb=counter_price_bnb,
                        price_usd=to_usd(counter_price_bnb, "BNB"),
                    )
                    if blocked_reason:
                        action_type = "LIVE_COUNTERBID_BLOCKED"
                        is_valid = False
                        error = blocked_reason
                        logger.warning("Blocked live counterbid for %s: %s", cfg.address, blocked_reason)
                        self.state.record_submit_event(
                            engine="counterbid",
                            action_type=action_type,
                            collection=cfg.address,
                            chain=resolved_chain,
                            price_bnb=counter_price_bnb,
                            status="blocked",
                            reason=blocked_reason,
                        )
                        self._send_live_notification(
                            action_type=action_type,
                            collection=cfg.address,
                            price_bnb=counter_price_bnb,
                            detail=blocked_reason,
                        )
                    else:
                        submit_result = self.okx.submit_offer(preview_payload)
                        action_type = "LIVE_COUNTERBID"
                        offer_id = str(submit_result.get("offer_id", "unknown"))
                        self.state.upsert_active_offer(
                            order_hash=offer_id,
                            collection=cfg.address,
                            chain=resolved_chain,
                            price_bnb=counter_price_bnb,
                            status="active",
                            current_floor=parasite_price_bnb if parasite_price_bnb > 0 else None,
                            preview_payload=preview_payload,
                        )
                        self.state.record_submit_event(
                            engine="counterbid",
                            action_type=action_type,
                            collection=cfg.address,
                            chain=resolved_chain,
                            price_bnb=counter_price_bnb,
                            status="submitted",
                            reason=f"offer_id={offer_id}",
                        )
                        self._send_live_notification(
                            action_type=action_type,
                            collection=cfg.address,
                            price_bnb=counter_price_bnb,
                            detail=f"offer_id={offer_id}",
                        )
            except Exception as exc:
                is_valid = False
                error = str(exc)
                action_type = "LIVE_COUNTERBID" if not effective_dry_run else "DRY_COUNTERBID"
                if not effective_dry_run:
                    logger.warning("Live counterbid failed for %s: %s", cfg.address, exc)
                    self.state.record_submit_event(
                        engine="counterbid",
                        action_type=action_type,
                        collection=cfg.address,
                        chain=resolved_chain,
                        price_bnb=counter_price_bnb,
                        status="failed",
                        reason=str(exc),
                    )

        task = CounterBidTask(
            collection=cfg.address,
            chain=resolved_chain,
            parasite_offer_bnb=parasite_price_bnb,
            counter_price_bnb=counter_price_bnb,
            reason=reason,
            valid=is_valid,
            error=error,
            parasite_maker=(parasite_offer.maker if parasite_offer else None),
            preview_payload=preview_payload,
            action_type=action_type,
            submit_result=submit_result,
        )
        return task, refresh_result

    def process_batch(
        self,
        *,
        chain: str | None = None,
        refresh: bool = False,
        sign_preview: bool = False,
        collection: str | None = None,
    ) -> BatchResult:
        resolved_chain = (chain or self.settings.execution_chain).lower()
        if resolved_chain != "bsc":
            raise ValueError(f"Only 'bsc' is supported in the execution track; got {resolved_chain!r}")
        if collection:
            task, refresh_result = self.process_single_collection(
                collection,
                chain=resolved_chain,
                refresh=refresh,
                sign_preview=sign_preview,
            )
            return BatchResult(
                chain=resolved_chain,
                tasks=[task],
                refresh_results=[refresh_result] if refresh_result is not None else [],
            )

        configs = self.config.list_collections(chain=resolved_chain, enabled_only=True)
        tasks: list[CounterBidTask] = []
        refresh_results: list[OfferRefreshResult] = []
        for cfg in configs:
            task, refresh_result = self.process_single_collection(
                cfg.address,
                chain=resolved_chain,
                refresh=refresh,
                sign_preview=sign_preview,
            )
            tasks.append(task)
            if refresh_result is not None:
                refresh_results.append(refresh_result)
        return BatchResult(chain=resolved_chain, tasks=tasks, refresh_results=refresh_results)

    def status(self, *, chain: str | None = None) -> dict[str, Any]:
        resolved_chain = (chain or self.settings.execution_chain).lower()
        integrity = self.state.audit_integrity().to_dict()
        rate_limits = self.state.get_rate_limit_snapshot(
            now=datetime.now(timezone.utc),
            max_live_offers_per_hour=self.settings.max_live_offers_per_hour,
            max_bnb_per_day=self.settings.max_bnb_per_day,
            submit_cooldown_seconds=self.settings.submit_cooldown_seconds,
        )
        return {
            "chain": resolved_chain,
            "dry_run": self.governor.effective_dry_run(),
            "configured_dry_run": self.settings.dry_run,
            "forced_dry_run": self.state.is_force_dry_run(),
            "live_arm": self.governor.get_live_arm_state(now=datetime.now(timezone.utc)),
            "integrity": integrity,
            "parasite_wallets": sorted(self.parasite_wallets),
            "collections": [asdict(config) for config in self.config.list_collections(chain=resolved_chain)],
            "rate_limits": {
                **rate_limits,
                "last_submit_at": rate_limits["last_submit_at"].isoformat() if rate_limits["last_submit_at"] else None,
            },
        }

    def _send_execution_notification(
        self,
        *,
        action_type: str,
        collection: str,
        price_bnb: float,
        detail: str,
    ) -> bool:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return False
        transport = StdlibHttpTransport(
            timeout=self.settings.okx_request_timeout,
            max_retries=self.settings.okx_max_retries,
            rate_limit_per_sec=self.settings.okx_rate_limit_per_sec,
        )
        notifier = TelegramNotifier(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            transport=transport,
        )
        event_id = f"live-counterbid:{collection.lower()}:{int(datetime.now(timezone.utc).timestamp())}"
        alert = AlertEnvelope(
            event=NFTEvent(
                event_id=event_id,
                market="okx",
                event_type="bid",
                collection=collection.lower(),
                token_id="0",
                contract_address=collection.lower(),
                price=price_bnb,
                currency="BNB",
                quantity=1,
                maker=self.settings.buyer_wallet_address,
                event_time=datetime.now(timezone.utc),
                raw_source="execution_live",
            ),
            decision=FilterDecision(
                event_id=event_id,
                passed=True,
                matched_rules=[action_type.lower()],
                reasons=[detail],
            ),
        )
        return notifier.send(alert).delivered

    def _send_live_notification(
        self,
        *,
        action_type: str,
        collection: str,
        price_bnb: float,
        detail: str,
    ) -> bool:
        return self._send_execution_notification(
            action_type=action_type,
            collection=collection,
            price_bnb=price_bnb,
            detail=detail,
        )

    @staticmethod
    def _dry_order_hash(
        *,
        collection: str,
        price_bnb: float,
        preview_payload: dict[str, Any] | None,
    ) -> str:
        seed = f"{collection.lower()}:{price_bnb:.18f}:COUNTERBID:{preview_payload}"
        import hashlib
        return "dryrun-cb-" + hashlib.sha256(seed.encode()).hexdigest()[:24]