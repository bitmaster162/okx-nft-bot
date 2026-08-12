from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Iterable

from okx_nft_bot.config import SUPPORTED_EXECUTION_CHAINS, Settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.undercutter.state import PositionState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KillSwitchChainResult:
    chain: str
    active_offers_seen: int
    exchange_seen: int
    live_cancelled: int
    local_cancelled: int
    already_gone: int
    failed: tuple[str, ...]
    exchange_lookup_failed: bool = False
    exchange_lookup_error: str | None = None
    fatal_error: str | None = None

    @property
    def failure_count(self) -> int:
        return len(self.failed) + (1 if self.fatal_error else 0)


@dataclass(slots=True)
class KillSwitchResult:
    activated_at: str
    chains: tuple[KillSwitchChainResult, ...]

    @property
    def total_failed(self) -> int:
        return sum(item.failure_count for item in self.chains)


def activate_multichain_killswitch(
    *,
    settings: Settings,
    state: PositionState | None = None,
    api: OKXAPIClient | None = None,
    chains: Iterable[str] | None = None,
) -> KillSwitchResult:
    """Disarm execution first, then cancel offers on every supported chain.

    A failure on one chain is isolated and does not prevent the remaining
    chains from being processed. This is intentionally fail-closed: forced
    dry-run and live disarm happen before the first exchange/RPC lookup.
    """
    resolved_state = state or PositionState(settings.execution_db_path)
    resolved_state.audit_integrity()

    activated_at = datetime.now(timezone.utc).isoformat()
    resolved_state.disarm_live(actor="telegram_killswitch", reason="telegram_killswitch")
    resolved_state.set_force_dry_run(True, reason="telegram_killswitch")
    resolved_state.set_runtime_value("killswitch_activated_at", activated_at)
    resolved_state.set_runtime_value("killswitch_source", "telegram")

    resolved_api = api or OKXAPIClient(settings=settings)
    resolved_chains = tuple(
        dict.fromkeys(
            str(chain).strip().lower()
            for chain in (chains or SUPPORTED_EXECUTION_CHAINS)
        )
    )
    results: list[KillSwitchChainResult] = []

    for chain in resolved_chains:
        try:
            result = _cancel_chain(
                state=resolved_state,
                api=resolved_api,
                chain=chain,
            )
        except Exception as exc:
            logger.exception("Kill switch fatal chain failure chain=%s", chain)
            failure = KillSwitchChainResult(
                chain=chain,
                active_offers_seen=0,
                exchange_seen=0,
                live_cancelled=0,
                local_cancelled=0,
                already_gone=0,
                failed=(),
                fatal_error=str(exc),
            )
            _record_chain_audit(resolved_state, failure)
            results.append(failure)
            continue
        results.append(result)

    return KillSwitchResult(activated_at=activated_at, chains=tuple(results))


def _exchange_collection(row: dict) -> str:
    collection = str(
        row.get("contractAddress")
        or row.get("collectionAddress")
        or row.get("collection")
        or row.get("collection_address")
        or "exchange_unknown"
    ).strip().lower()
    return collection or "exchange_unknown"


def _unidentified_exchange_id(row: dict) -> str:
    canonical = json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:20]
    return f"exchange_unidentified_{digest}"


def _cancel_chain(
    *,
    state: PositionState,
    api: OKXAPIClient,
    chain: str,
) -> KillSwitchChainResult:
    active_offers = state.get_active_offers(chain=chain)
    exchange_lookup_failed = False
    exchange_lookup_error: str | None = None
    exchange_order_hashes: list[str] = []
    exchange_order_params: dict[str, dict] = {}
    exchange_order_collections: dict[str, str] = {}
    unidentified_exchange_orders: dict[str, str] = {}

    try:
        seen: set[str] = set()
        for row in api.get_my_offers(chain=chain, require_all_endpoints=True):
            order_hash = str(
                row.get("offerId") or row.get("orderHash") or row.get("id") or ""
            ).strip()
            if not order_hash:
                quarantine_id = _unidentified_exchange_id(row)
                unidentified_exchange_orders.setdefault(
                    quarantine_id,
                    _exchange_collection(row),
                )
                continue
            if order_hash in seen:
                continue
            seen.add(order_hash)
            exchange_order_hashes.append(order_hash)
            exchange_order_collections[order_hash] = _exchange_collection(row)
            proto = row.get("protocolData", {})
            if isinstance(proto, str):
                try:
                    proto = json.loads(proto)
                except Exception:
                    proto = {}
            params = proto.get("parameters") if isinstance(proto, dict) else None
            if params:
                exchange_order_params[order_hash] = params
    except Exception as exc:
        exchange_lookup_failed = True
        exchange_lookup_error = str(exc)
        logger.warning("Kill switch exchange lookup failed chain=%s: %s", chain, exc)
        exchange_order_hashes = [
            offer.order_hash
            for offer in active_offers
            if not offer.order_hash.startswith("dryrun-")
        ]

    live_cancelled = 0
    local_cancelled = 0
    already_gone = 0
    failed: list[str] = [
        f"{quarantine_id}:missing_order_id"
        for quarantine_id in unidentified_exchange_orders
    ]
    failed_order_hashes: list[str] = []
    successful_live_cancels: set[str] = set()

    for order_hash in exchange_order_hashes:
        try:
            ok = api.cancel_offer(
                order_hash,
                chain=chain,
                order_params=exchange_order_params.get(order_hash),
            )
        except Exception as exc:
            failed_order_hashes.append(order_hash)
            failed.append(f"{order_hash}:{exc}")
            logger.warning(
                "Kill switch cancel failed chain=%s order=%s: %s",
                chain,
                order_hash,
                exc,
            )
            continue
        if ok:
            live_cancelled += 1
            successful_live_cancels.add(order_hash)
        else:
            failed_order_hashes.append(order_hash)
            failed.append(f"{order_hash}:cancel_failed")

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
            state.mark_offer_status(
                order_hash=offer.order_hash,
                status="killswitch_failed",
            )
            continue
        if not exchange_lookup_failed:
            if state.mark_offer_status(order_hash=offer.order_hash, status="cancelled"):
                already_gone += 1

    active_hash_set = {offer.order_hash for offer in active_offers}
    for order_hash in failed_order_hashes:
        if order_hash in active_hash_set:
            continue
        # R28: exchange discovery can reveal live orders missing from local
        # active_offers. UPDATE-only mark_offer_status cannot quarantine such an
        # order. Persist an explicit sentinel zombie row so subsequent governor
        # checks see killswitch_failed and veto new live submissions.
        state.upsert_active_offer(
            order_hash=order_hash,
            collection=exchange_order_collections.get(order_hash, "exchange_unknown"),
            chain=chain,
            price_bnb=0.0,
            status="killswitch_failed",
        )

    # R29: an exchange row without any durable order identifier is not a clean
    # cancellation result. It cannot safely be sent to cancel_offer, so persist
    # a deterministic quarantine fingerprint instead. This keeps the governor
    # vetoed until the unidentified exchange object is investigated manually.
    for quarantine_id, collection in unidentified_exchange_orders.items():
        state.upsert_active_offer(
            order_hash=quarantine_id,
            collection=collection,
            chain=chain,
            price_bnb=0.0,
            status="killswitch_failed",
        )

    result = KillSwitchChainResult(
        chain=chain,
        active_offers_seen=len(active_offers),
        exchange_seen=len(exchange_order_hashes) + len(unidentified_exchange_orders),
        live_cancelled=live_cancelled,
        local_cancelled=local_cancelled,
        already_gone=already_gone,
        failed=tuple(failed),
        exchange_lookup_failed=exchange_lookup_failed,
        exchange_lookup_error=exchange_lookup_error,
    )
    _record_chain_audit(state, result)
    return result


def _record_chain_audit(state: PositionState, result: KillSwitchChainResult) -> None:
    state.record_submit_event(
        engine="runtime",
        action_type="KILLSWITCH",
        collection="*",
        chain=result.chain,
        price_bnb=None,
        status="killswitch",
        reason=(
            f"exchange_seen={result.exchange_seen};live_cancelled={result.live_cancelled};"
            f"local_cancelled={result.local_cancelled};already_gone={result.already_gone};"
            f"failed={result.failure_count};"
            f"exchange_lookup_failed={1 if result.exchange_lookup_failed else 0};"
            f"fatal={1 if result.fatal_error else 0}"
        ),
    )
    errors = list(result.failed)
    if result.fatal_error:
        errors.append(f"fatal:{result.fatal_error}")
    state.log_action(
        action_type="KILLSWITCH",
        collection="*",
        chain=result.chain,
        order_hash=None,
        old_price_bnb=None,
        new_price_bnb=None,
        reason="CRITICAL: operator kill switch activated",
        executed=result.failure_count == 0,
        error="; ".join(errors) if errors else None,
        payload={
            "exchange_seen": result.exchange_seen,
            "live_cancelled": result.live_cancelled,
            "local_cancelled": result.local_cancelled,
            "already_gone": result.already_gone,
            "exchange_lookup_failed": result.exchange_lookup_failed,
            "exchange_lookup_error": result.exchange_lookup_error,
            "failed": list(result.failed),
            "fatal_error": result.fatal_error,
        },
    )


def format_killswitch_result(result: KillSwitchResult) -> str:
    lines = [
        "killswitch_activated",
        "dry_run=true",
        f"chains={','.join(item.chain for item in result.chains)}",
    ]
    for item in result.chains:
        if item.fatal_error:
            lines.append(f"chain={item.chain} fatal_error={item.fatal_error}")
            continue
        lines.append(
            f"chain={item.chain} active_offers_seen={item.active_offers_seen} "
            f"exchange_seen={item.exchange_seen} live_cancelled={item.live_cancelled} "
            f"local_cancelled={item.local_cancelled} already_gone={item.already_gone} "
            f"failed={len(item.failed)} zombies={len(item.failed)}"
        )
    lines.append(f"total_failed={result.total_failed}")
    return "\n".join(lines)
