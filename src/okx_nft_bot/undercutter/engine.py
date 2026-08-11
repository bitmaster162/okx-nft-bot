from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings, validate_execution_chain
from okx_nft_bot.counterbid.config import CounterbidConfigManager
from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.notifiers.base import AlertEnvelope
from okx_nft_bot.notifiers.telegram import TelegramNotifier
from okx_nft_bot.signing import preview_counterbid
from okx_nft_bot.undercutter.monitor import OfferMonitor
from okx_nft_bot.undercutter.state import ActiveOffer, PositionState
from okx_nft_bot.undercutter.strategy import UndercutStrategy

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UndercutAction:
    action_type: str
    collection: str
    chain: str
    old_price_bnb: float | None
    new_price_bnb: float | None
    reason: str
    executed: bool = False
    error: str | None = None
    order_hash: str | None = None
    payload: dict[str, Any] | None = None
    submit_result: dict[str, Any] | None = None


class UndercutEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        offer_client: OKXAPIClient | None = None,
        state: PositionState | None = None,
        config_manager: CounterbidConfigManager | None = None,
    ) -> None:
        self.settings = settings
        self.offer_client = offer_client or OKXAPIClient(settings=settings)
        self.state = state or PositionState(settings.execution_db_path)
        self.config_manager = config_manager or CounterbidConfigManager(settings.execution_db_path)
        self.governor = ExecutionGovernor(settings=settings, state=self.state, api_client=self.offer_client)
        self.monitor = OfferMonitor(self.offer_client)
        self.strategy = UndercutStrategy(settings, self.config_manager)

    def run_cycle(self, *, chain: str | None = None, refresh: bool = False) -> list[UndercutAction]:
        resolved_chain = validate_execution_chain(chain or self.settings.execution_chain)
        actions: list[UndercutAction] = []
        active_offers = self.state.get_active_offers(chain=resolved_chain)

        for offer in active_offers:
            market_offers = self.monitor.fetch_offers(
                offer.collection,
                chain=resolved_chain,
                refresh=refresh,
            )
            best_offer = self.monitor.get_best_offer(market_offers)
            current_floor = float(best_offer.price or 0.0) if best_offer and best_offer.price is not None else None
            self.state.touch_offer(order_hash=offer.order_hash, current_floor=current_floor)

            best_non_ours = self.monitor.get_best_offer_excluding(market_offers, self.settings.buyer_wallet_address)
            defended = False
            if best_non_ours and float(best_non_ours.price or 0.0) > offer.price_bnb:
                new_price = self.strategy.calculate_defense_price(
                    our_price=offer.price_bnb,
                    competitor_price=float(best_non_ours.price or 0.0),
                    collection=offer.collection,
                )
                actions.append(
                    UndercutAction(
                        action_type="DEFENSE",
                        collection=offer.collection,
                        chain=resolved_chain,
                        old_price_bnb=offer.price_bnb,
                        new_price_bnb=new_price,
                        reason=f"Outbid by {(best_non_ours.maker or 'unknown')[:10]} at {float(best_non_ours.price or 0.0):.6f}",
                        order_hash=offer.order_hash,
                    )
                )
                defended = True

            refreshed_offer = ActiveOffer(
                order_hash=offer.order_hash,
                collection=offer.collection,
                chain=offer.chain,
                price_bnb=offer.price_bnb,
                status=offer.status,
                placed_at=offer.placed_at,
                last_checked_at=offer.last_checked_at,
                current_floor=current_floor,
                preview_payload=offer.preview_payload,
            )
            if not defended and self.strategy.should_withdraw(refreshed_offer):
                actions.append(
                    UndercutAction(
                        action_type="WITHDRAW",
                        collection=offer.collection,
                        chain=resolved_chain,
                        old_price_bnb=offer.price_bnb,
                        new_price_bnb=None,
                        reason="Offer stale or too far above current best offer",
                        order_hash=offer.order_hash,
                    )
                )

        tracked_collections = [config.address for config in self.config_manager.list_collections(chain=resolved_chain, enabled_only=True)]
        attack_targets = self.strategy.find_attack_targets(
            tracked_collections=tracked_collections,
            exclude={offer.collection for offer in active_offers},
            fetch_offers=lambda collection: self.monitor.fetch_offers(
                collection,
                chain=resolved_chain,
                refresh=refresh,
            ),
        )
        for target in attack_targets:
            actions.append(
                UndercutAction(
                    action_type="ATTACK",
                    collection=target.collection,
                    chain=resolved_chain,
                    old_price_bnb=None,
                    new_price_bnb=target.suggested_price,
                    reason=target.reason,
                )
            )

        for action in actions:
            self._apply_action(action)
        return actions

    def _live_withdraw_block_reason(self) -> str | None:
        """Gate ordinary live withdrawals without applying offer spend/rate caps.

        A normal undercutter withdrawal is still an external execution effect, so
        it must not run while configured/forced dry-run is active or outside the
        explicit live-arm window.  Emergency killswitch cancellation deliberately
        uses its separate coordinator and does not call this helper.
        """
        if self.governor.effective_dry_run():
            return "dry_run_enabled"
        arm_state = self.governor.get_live_arm_state(now=datetime.now(timezone.utc))
        if not arm_state["armed"]:
            if arm_state.get("expires_at"):
                return "live arm expired"
            return "live arm required"
        return None

    def _apply_action(self, action: UndercutAction) -> None:
        effective_dry_run = self.governor.effective_dry_run()
        if action.action_type == "WITHDRAW":
            order_hash = action.order_hash or ""
            is_dry_run_offer = order_hash.startswith("dryrun-")
            logged_action_type = "DRY_WITHDRAW" if is_dry_run_offer else "LIVE_WITHDRAW"

            if is_dry_run_offer:
                action.executed = self.state.mark_offer_status(
                    order_hash=order_hash,
                    status="cancelled",
                )
                if not action.executed:
                    action.error = "Offer not found"
            elif not order_hash:
                action.executed = False
                action.error = "Offer not found"
            else:
                blocked_reason = self._live_withdraw_block_reason()
                if blocked_reason:
                    action.executed = False
                    action.error = blocked_reason
                    self.state.record_submit_event(
                        engine="undercutter",
                        action_type=f"{logged_action_type}_BLOCKED",
                        collection=action.collection,
                        chain=action.chain,
                        price_bnb=action.old_price_bnb,
                        status="blocked",
                        reason=blocked_reason,
                    )
                else:
                    try:
                        # Fetching order parameters is read-only. Re-check the gate
                        # immediately before the external cancel to narrow a race
                        # with disarm/killswitch activation.
                        order_params = self._fetch_order_params(order_hash, action.chain)
                        blocked_reason = self._live_withdraw_block_reason()
                        if blocked_reason:
                            action.executed = False
                            action.error = blocked_reason
                            self.state.record_submit_event(
                                engine="undercutter",
                                action_type=f"{logged_action_type}_BLOCKED",
                                collection=action.collection,
                                chain=action.chain,
                                price_bnb=action.old_price_bnb,
                                status="blocked",
                                reason=blocked_reason,
                            )
                        else:
                            action.executed = self.offer_client.cancel_offer(
                                order_hash,
                                chain=action.chain or self.settings.execution_chain,
                                order_params=order_params,
                            )
                            if action.executed:
                                local_marked = self.state.mark_offer_status(
                                    order_hash=order_hash,
                                    status="cancelled",
                                )
                                self.state.record_submit_event(
                                    engine="undercutter",
                                    action_type=logged_action_type,
                                    collection=action.collection,
                                    chain=action.chain,
                                    price_bnb=action.old_price_bnb,
                                    status="cancelled",
                                    reason=f"order_hash={order_hash};local_marked={1 if local_marked else 0}",
                                )
                                self._send_live_notification(
                                    action_type=logged_action_type,
                                    collection=action.collection,
                                    price_bnb=action.old_price_bnb,
                                    detail=f"order_hash={order_hash}",
                                )
                            else:
                                action.error = "Exchange cancel failed"
                                self.state.record_submit_event(
                                    engine="undercutter",
                                    action_type=logged_action_type,
                                    collection=action.collection,
                                    chain=action.chain,
                                    price_bnb=action.old_price_bnb,
                                    status="failed",
                                    reason=action.error,
                                )
                    except Exception as exc:
                        action.executed = False
                        action.error = str(exc)
                        logger.warning("Live withdraw failed for %s: %s", action.collection, exc)
                        self.state.record_submit_event(
                            engine="undercutter",
                            action_type=logged_action_type,
                            collection=action.collection,
                            chain=action.chain,
                            price_bnb=action.old_price_bnb,
                            status="failed",
                            reason=str(exc),
                        )

            self.state.log_action(
                action_type=logged_action_type,
                collection=action.collection,
                chain=action.chain,
                order_hash=action.order_hash,
                old_price_bnb=action.old_price_bnb,
                new_price_bnb=action.new_price_bnb,
                reason=action.reason,
                executed=action.executed,
                error=action.error,
                payload=action.payload,
            )
            return

        # ── MIN PRICE SAFETY CHECK ──
        import os as _os
        _min_price = float(_os.environ.get("UNDERCUTTER_MIN_PRICE_BNB", "0.0001"))
        if action.new_price_bnb is not None and action.new_price_bnb < _min_price:
            logger.warning(
                "Rejected %s for %s: price %.8f BNB below minimum %.8f",
                action.action_type, action.collection, action.new_price_bnb, _min_price,
            )
            action.executed = False
            action.error = f"Price {action.new_price_bnb:.8f} below min {_min_price:.8f}"
            self.state.log_action(
                action_type=f"REJECTED_{action.action_type}",
                collection=action.collection,
                chain=action.chain,
                order_hash=action.order_hash,
                old_price_bnb=action.old_price_bnb,
                new_price_bnb=action.new_price_bnb,
                reason=action.error,
                executed=False,
                error=action.error,
                payload=None,
            )
            return

        preview_payload: dict[str, Any] | None = None
        preview_error: str | None = None
        if self.settings.buyer_wallet_address and self.settings.buyer_wallet_private_key and action.new_price_bnb is not None:
            try:
                preview_payload = preview_counterbid(
                    settings=self.settings,
                    collection=action.collection,
                    price_bnb=action.new_price_bnb,
                    chain=action.chain,
                )
            except Exception as exc:
                preview_error = str(exc)

        previous_order_hash = action.order_hash
        new_order_hash = self._order_hash(action.collection, action.new_price_bnb or 0.0, action.action_type, preview_payload)
        logged_action_type = f"{'DRY' if effective_dry_run else 'LIVE'}_{action.action_type}"
        action.executed = preview_error is None
        action.error = preview_error
        action.payload = preview_payload
        action.order_hash = new_order_hash

        if action.executed:
            if effective_dry_run:
                self._retire_previous_offer_for_defense(
                    action_type=action.action_type,
                    previous_order_hash=previous_order_hash,
                    effective_dry_run=effective_dry_run,
                    chain=action.chain or "",
                )
                self.state.upsert_active_offer(
                    order_hash=new_order_hash,
                    collection=action.collection,
                    chain=action.chain,
                    price_bnb=float(action.new_price_bnb or 0.0),
                    status="active",
                    current_floor=action.old_price_bnb,
                    preview_payload=preview_payload,
                )
            else:
                try:
                    from okx_nft_bot.prices import to_usd

                    _new_price_bnb = float(action.new_price_bnb or 0.0)
                    blocked_reason = self.governor.check_live_submit_allowed(
                        action_type=logged_action_type,
                        collection=action.collection,
                        chain=action.chain,
                        price_bnb=_new_price_bnb,
                        price_usd=to_usd(_new_price_bnb, "BNB"),
                    )
                    if blocked_reason:
                        action.executed = False
                        action.error = blocked_reason
                        logger.warning("Blocked live %s for %s: %s", action.action_type.lower(), action.collection, blocked_reason)
                        self.state.record_submit_event(
                            engine="undercutter",
                            action_type=f"{logged_action_type}_BLOCKED",
                            collection=action.collection,
                            chain=action.chain,
                            price_bnb=float(action.new_price_bnb or 0.0),
                            status="blocked",
                            reason=blocked_reason,
                        )
                        self._send_live_notification(
                            action_type=f"{logged_action_type}_BLOCKED",
                            collection=action.collection,
                            price_bnb=action.new_price_bnb,
                            detail=blocked_reason,
                        )
                    else:
                        # A6: submit-then-retire. If we retire the old offer
                        # first and submit then fails, we sit defenseless until
                        # next cycle (up to undercut_interval_seconds). Submit
                        # new first; retire old only after we confirmed the new
                        # offer reached the exchange.
                        action.submit_result = self.offer_client.submit_offer(preview_payload or {})
                        submitted_offer_id = action.submit_result.get("offer_id")
                        # A5: require real offer_id from exchange. Empty/None
                        # means reconcile would see a "dryrun-" hash for a live
                        # order and ignore it (orphan state). Skip the state
                        # write; next reconcile will pull it from the exchange.
                        if submitted_offer_id:
                            action.order_hash = str(submitted_offer_id)
                            self.state.upsert_active_offer(
                                order_hash=action.order_hash,
                                collection=action.collection,
                                chain=action.chain,
                                price_bnb=float(action.new_price_bnb or 0.0),
                                status="active",
                                current_floor=action.old_price_bnb,
                                preview_payload=preview_payload,
                            )
                            # Now retire the old offer (new one is live)
                            self._retire_previous_offer_for_defense(
                                action_type=action.action_type,
                                previous_order_hash=previous_order_hash,
                                effective_dry_run=effective_dry_run,
                                chain=action.chain or "",
                            )
                        else:
                            logger.warning(
                                "submit_offer returned empty offer_id for %s — "
                                "skipping state write AND keeping old offer "
                                "(reconcile will recover)",
                                action.collection,
                            )
                        self.state.record_submit_event(
                            engine="undercutter",
                            action_type=logged_action_type,
                            collection=action.collection,
                            chain=action.chain,
                            price_bnb=float(action.new_price_bnb or 0.0),
                            status="submitted",
                            reason=f"offer_id={action.order_hash}",
                        )
                        self._send_live_notification(
                            action_type=logged_action_type,
                            collection=action.collection,
                            price_bnb=action.new_price_bnb,
                            detail=f"offer_id={action.order_hash}",
                        )
                except Exception as exc:
                    action.executed = False
                    action.error = str(exc)
                    logger.warning("Live %s failed for %s: %s", action.action_type.lower(), action.collection, exc)
                    self.state.record_submit_event(
                        engine="undercutter",
                        action_type=logged_action_type,
                        collection=action.collection,
                        chain=action.chain,
                        price_bnb=float(action.new_price_bnb or 0.0),
                        status="failed",
                        reason=str(exc),
                    )

        self.state.log_action(
            action_type=logged_action_type,
            collection=action.collection,
            chain=action.chain,
            order_hash=action.order_hash,
            old_price_bnb=action.old_price_bnb,
            new_price_bnb=action.new_price_bnb,
            reason=action.reason,
            executed=action.executed,
            error=action.error,
            payload=action.submit_result or action.payload,
        )

    def status(self, *, chain: str | None = None) -> dict[str, Any]:
        resolved_chain = validate_execution_chain(chain or self.settings.execution_chain)
        integrity = self.state.audit_integrity().to_dict()
        rate_limits = self.governor.get_rate_limit_snapshot(chain=resolved_chain, now=datetime.now(timezone.utc))
        return {
            "chain": resolved_chain,
            "dry_run": self.governor.effective_dry_run(),
            "configured_dry_run": self.settings.dry_run,
            "forced_dry_run": self.state.is_force_dry_run(),
            "live_arm": self.governor.get_live_arm_state(now=datetime.now(timezone.utc)),
            "integrity": integrity,
            "active_offers": self.state.list_active_offers(chain=resolved_chain),
            "recent_actions": self.state.list_action_history(limit=10),
            "tracked_collections": [asdict(config) for config in self.config_manager.list_collections(chain=resolved_chain)],
            "rate_limits": {
                **rate_limits,
                "last_submit_at": rate_limits["last_submit_at"].isoformat() if rate_limits["last_submit_at"] else None,
            },
        }

    @staticmethod
    def _order_hash(
        collection: str,
        price_bnb: float,
        action_type: str,
        preview_payload: dict[str, Any] | None,
    ) -> str:
        seed = f"{collection.lower()}:{price_bnb:.18f}:{action_type}:{preview_payload or {}}"
        return "dryrun-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    def _send_execution_notification(
        self,
        *,
        action_type: str,
        collection: str,
        price_bnb: float | None,
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
        event_id = f"live-undercut:{collection.lower()}:{int(datetime.now(timezone.utc).timestamp())}"
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
        price_bnb: float | None,
        detail: str,
    ) -> bool:
        return self._send_execution_notification(
            action_type=action_type,
            collection=collection,
            price_bnb=price_bnb,
            detail=detail,
        )

    def _retire_previous_offer_for_defense(
        self,
        *,
        action_type: str,
        previous_order_hash: str | None,
        effective_dry_run: bool,
        chain: str = "",
    ) -> None:
        if action_type != "DEFENSE" or not previous_order_hash:
            return
        if previous_order_hash.startswith("dryrun-"):
            self.state.mark_offer_status(order_hash=previous_order_hash, status="retired")
            logger.info("Retired dry-run offer %s during defense re-bid", previous_order_hash)
            return
        if effective_dry_run:
            # A preview/dry-run must never hide a real exchange offer from local
            # state. Keeping it active is required so reconcile/killswitch can
            # still see and cancel the live exposure if needed.
            logger.info(
                "Keeping live offer %s active during dry-run defense preview",
                previous_order_hash,
            )
            return
        resolved_chain = chain or self.settings.execution_chain
        try:
            _order_params = self._fetch_order_params(previous_order_hash, resolved_chain)
            cancelled = self.offer_client.cancel_offer(
                previous_order_hash,
                chain=resolved_chain,
                order_params=_order_params,
            )
            if cancelled:
                self.state.mark_offer_status(order_hash=previous_order_hash, status="cancelled")
                logger.info("Cancelled old offer %s before defense re-bid", previous_order_hash)
            else:
                logger.warning("Failed to cancel old offer %s during defense", previous_order_hash)
        except Exception as exc:
            logger.warning("Failed to cancel old offer %s during defense: %s", previous_order_hash, exc)

    def _fetch_order_params(self, order_hash: str, chain: str) -> dict | None:
        """Try to fetch protocolData.parameters for an order to enable on-chain cancel."""
        try:
            import json as _json
            for rec in self.offer_client.get_my_offers(chain=chain or self.settings.execution_chain):
                oid = str(rec.get("orderId") or rec.get("orderHash") or rec.get("id") or "")
                if oid == order_hash:
                    proto = rec.get("protocolData", {})
                    if isinstance(proto, str):
                        try:
                            proto = _json.loads(proto)
                        except Exception:
                            proto = {}
                    return proto.get("parameters") if isinstance(proto, dict) else None
        except Exception as exc:
            logger.debug("_fetch_order_params(%s): %s", order_hash[:14], exc)
        return None
