from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Iterable

from okx_nft_bot.clients.opensea_killswitch import OpenSeaKillSwitchClient
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
    local_state_lookup_failed: bool = False
    local_state_lookup_error: str | None = None
    local_state_persistence_failed: bool = False
    local_state_persistence_error: str | None = None

    @property
    def failure_count(self) -> int:
        state_degraded = self.local_state_lookup_failed or self.local_state_persistence_failed
        exchange_degraded = self.exchange_lookup_failed and not self.fatal_error
        return (
            len(self.failed)
            + (1 if state_degraded else 0)
            + (1 if exchange_degraded else 0)
            + (1 if self.fatal_error else 0)
        )


@dataclass(slots=True)
class KillSwitchResult:
    activated_at: str
    chains: tuple[KillSwitchChainResult, ...]
    preflight_error: str | None = None

    @property
    def total_failed(self) -> int:
        return sum(item.failure_count for item in self.chains) + (1 if self.preflight_error else 0)


class _UnavailableOKXAPI:
    """Duck-typed fail-closed OKX boundary used after constructor failure."""

    def __init__(self, error: str) -> None:
        self.error = error

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        raise RuntimeError(self.error)

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        raise RuntimeError(self.error)


def activate_multichain_killswitch(
    *,
    settings: Settings,
    state: PositionState | None = None,
    api: OKXAPIClient | None = None,
    opensea_api: OpenSeaKillSwitchClient | None = None,
    chains: Iterable[str] | None = None,
) -> KillSwitchResult:
    """Latch local safety, then best-effort persist it and cancel every chain.

    Process-local dry-run is set before any state access. If local execution
    state cannot even be constructed, the kill switch degrades to exchange-only
    cancellation rather than abandoning the emergency action.
    """
    settings.dry_run = True
    activated_at = datetime.now(timezone.utc).isoformat()
    preflight_errors: list[str] = []

    resolved_state: PositionState | None = state
    state_init_error: str | None = None
    if resolved_state is None:
        try:
            resolved_state = PositionState(settings.execution_db_path)
        except Exception as exc:
            state_init_error = str(exc)
            preflight_errors.append(f"state_init: {exc}")
            logger.exception(
                "Kill switch local state initialization failed; continuing exchange-only cancellation"
            )

    # R32: persistent safety writes and integrity audit are independent. They are
    # attempted only when a local state store exists; state-init failure is
    # already represented in preflight_error and must not suppress cancellation.
    if resolved_state is not None:
        try:
            resolved_state.disarm_live(
                actor="telegram_killswitch",
                reason="telegram_killswitch",
            )
        except Exception as exc:
            preflight_errors.append(f"disarm_live: {exc}")
            logger.exception(
                "Kill switch persistent live disarm failed; continuing emergency cancellation"
            )

        try:
            resolved_state.set_force_dry_run(True, reason="telegram_killswitch")
        except Exception as exc:
            preflight_errors.append(f"set_force_dry_run: {exc}")
            logger.exception(
                "Kill switch persistent force-dry-run failed; continuing emergency cancellation"
            )

        for key, value in (
            ("killswitch_activated_at", activated_at),
            ("killswitch_source", "telegram"),
        ):
            try:
                resolved_state.set_runtime_value(key, value)
            except Exception as exc:
                preflight_errors.append(f"set_runtime_value[{key}]: {exc}")
                logger.exception(
                    "Kill switch runtime metadata persistence failed key=%s; continuing emergency cancellation",
                    key,
                )

        try:
            resolved_state.audit_integrity()
        except Exception as exc:
            preflight_errors.append(str(exc))
            logger.exception(
                "Kill switch integrity preflight failed after safety latch; continuing cancellation"
            )

    preflight_error = "; ".join(preflight_errors) or None
    resolved_chains = tuple(
        dict.fromkeys(
            str(chain).strip().lower()
            for chain in (chains or SUPPORTED_EXECUTION_CHAINS)
        )
    )

    # R46: OpenSea cancellation is a separate marketplace boundary. Constructing
    # this adapter has no network or signing effect; credentials are validated
    # only if a tracked OpenSea order actually reaches the cancel boundary.
    resolved_opensea_api = opensea_api
    if resolved_opensea_api is None:
        try:
            resolved_opensea_api = OpenSeaKillSwitchClient(settings=settings)
        except Exception:
            resolved_opensea_api = None
            logger.exception(
                "Kill switch OpenSea adapter initialization failed; tracked OpenSea orders will remain quarantined"
            )

    # R47: an OKX constructor failure is fatal for the OKX marketplace boundary,
    # but it must not suppress independent cancellation adapters. Preserve the
    # R37 fatal result while routing every chain through _cancel_chain so tracked
    # OpenSea exposures can still be cancelled and persisted first.
    resolved_api = api
    okx_api_init_error: str | None = None
    if resolved_api is None:
        try:
            resolved_api = OKXAPIClient(settings=settings)
        except Exception as exc:
            okx_api_init_error = f"api_init: {exc}"
            resolved_api = _UnavailableOKXAPI(okx_api_init_error)
            logger.exception(
                "Kill switch OKX API initialization failed; continuing independent marketplace cancellation"
            )

    results: list[KillSwitchChainResult] = []

    for chain in resolved_chains:
        try:
            result = _cancel_chain(
                state=resolved_state,
                api=resolved_api,
                opensea_api=resolved_opensea_api,
                chain=chain,
                state_unavailable_error=state_init_error,
                fatal_error=okx_api_init_error,
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
            if resolved_state is not None:
                _record_chain_audit_best_effort(resolved_state, failure)
            results.append(failure)
            continue
        results.append(result)

    return KillSwitchResult(
        activated_at=activated_at,
        chains=tuple(results),
        preflight_error=preflight_error,
    )


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


def _local_offer_marketplace(offer: object) -> str:
    """Return explicit local marketplace; legacy rows default to OKX.

    active_offers predates marketplace-aware inventory. Existing rows without a
    tag are therefore OKX by construction. A present non-OKX tag must never be
    routed through OKX cancellation merely because it shares the same chain.
    """
    payload = getattr(offer, "preview_payload", None)
    if not isinstance(payload, dict):
        return "okx"
    value = str(payload.get("marketplace") or "").strip().lower()
    return value or "okx"


def _cancel_chain(
    *,
    state: PositionState | None,
    api: OKXAPIClient,
    chain: str,
    opensea_api: OpenSeaKillSwitchClient | None = None,
    state_unavailable_error: str | None = None,
    fatal_error: str | None = None,
) -> KillSwitchChainResult:
    # R33/R34: local SQLite is fallback/accounting input, not a prerequisite for
    # exchange discovery or cancellation.
    local_state_lookup_failed = False
    local_state_lookup_error: str | None = None
    if state is None:
        active_offers = []
    else:
        try:
            active_offers = state.get_active_offers(chain=chain)
        except Exception as exc:
            active_offers = []
            local_state_lookup_failed = True
            local_state_lookup_error = str(exc)
            logger.warning(
                "Kill switch local state lookup failed chain=%s: %s; continuing exchange cancellation",
                chain,
                exc,
            )

    # R40/R46: local inventory is marketplace-aware. Legacy/explicit OKX rows
    # only ever reach OKX. Tracked OpenSea rows have a separately authenticated
    # SignedZone off-chain cancel boundary. Any other marketplace remains
    # unsupported and therefore quarantined fail-closed.
    okx_active_offers = []
    opensea_active_offers = []
    unsupported_active_offers: list[tuple[object, str]] = []
    for offer in active_offers:
        marketplace = _local_offer_marketplace(offer)
        if marketplace == "okx":
            okx_active_offers.append(offer)
        elif marketplace == "opensea":
            opensea_active_offers.append(offer)
        else:
            unsupported_active_offers.append((offer, marketplace))

    live_cancelled = 0
    local_cancelled = 0
    already_gone = 0
    failed: list[str] = []
    failed_order_hashes: list[str] = []
    successful_live_cancels: set[str] = set()
    opensea_failed_hashes: set[str] = set()

    # R46: cancel already-tracked OpenSea orders independently of OKX discovery.
    # The adapter itself single-attempts the effectful POST and requires exact
    # order readback to confirm cancellation before returning True.
    for offer in opensea_active_offers:
        order_hash = offer.order_hash
        if opensea_api is None:
            opensea_failed_hashes.add(order_hash)
            failed.append(f"{order_hash}:opensea_cancel_unavailable")
            continue
        try:
            ok = opensea_api.cancel_offer(order_hash, chain=chain)
        except Exception as exc:
            opensea_failed_hashes.add(order_hash)
            failed.append(f"{order_hash}:opensea_cancel_failed:{exc}")
            logger.warning(
                "Kill switch OpenSea cancel failed chain=%s order=%s: %s",
                chain,
                order_hash,
                exc,
            )
            continue
        if ok:
            live_cancelled += 1
            successful_live_cancels.add(order_hash)
        else:
            opensea_failed_hashes.add(order_hash)
            failed.append(f"{order_hash}:opensea_cancel_failed")

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
        if state is None:
            return KillSwitchChainResult(
                chain=chain,
                active_offers_seen=0,
                exchange_seen=0,
                live_cancelled=live_cancelled,
                local_cancelled=0,
                already_gone=0,
                failed=tuple(failed),
                exchange_lookup_failed=True,
                exchange_lookup_error=exchange_lookup_error,
                fatal_error=(
                    fatal_error
                    or "exchange lookup failed while local state unavailable: "
                    f"{exchange_lookup_error}"
                ),
            )
        exchange_order_hashes = [
            offer.order_hash
            for offer in okx_active_offers
            if not offer.order_hash.startswith("dryrun-")
        ]

    failed.extend(
        f"{quarantine_id}:missing_order_id"
        for quarantine_id in unidentified_exchange_orders
    )
    for offer, marketplace in unsupported_active_offers:
        failed.append(f"{offer.order_hash}:{marketplace}_cancel_unavailable")

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

    # R35: after exchange effects, local state persistence is secondary. Failure
    # to mark/upsert must not erase real exchange_seen/live_cancelled/failed data.
    local_state_persistence_errors: list[str] = []

    def note_state_persistence_failure(operation: str, exc: Exception) -> None:
        message = f"{operation}: {exc}"
        local_state_persistence_errors.append(message)
        logger.warning(
            "Kill switch local state persistence failed chain=%s operation=%s: %s",
            chain,
            operation,
            exc,
        )

    if state is not None:
        # R46: an OpenSea row is cleared only after the adapter has confirmed the
        # exact order is cancelled. Every other outcome remains a zombie.
        for offer in opensea_active_offers:
            status = (
                "cancelled"
                if offer.order_hash in successful_live_cancels
                else "killswitch_failed"
            )
            try:
                state.mark_offer_status(
                    order_hash=offer.order_hash,
                    status=status,
                )
            except Exception as exc:
                note_state_persistence_failure(
                    f"mark_opensea_{status}[{offer.order_hash}]",
                    exc,
                )

        # Unsupported marketplace exposure is not "already gone" merely because
        # OKX inventory cannot see it. Persist an explicit zombie state so every
        # later live-submit boundary remains fail-closed.
        for offer, marketplace in unsupported_active_offers:
            try:
                state.mark_offer_status(
                    order_hash=offer.order_hash,
                    status="killswitch_failed",
                )
            except Exception as exc:
                note_state_persistence_failure(
                    f"mark_{marketplace}_killswitch_failed[{offer.order_hash}]",
                    exc,
                )

        failed_hash_set = set(failed_order_hashes)
        for offer in okx_active_offers:
            if offer.order_hash.startswith("dryrun-"):
                try:
                    marked = state.mark_offer_status(
                        order_hash=offer.order_hash,
                        status="cancelled",
                    )
                except Exception as exc:
                    note_state_persistence_failure(
                        f"mark_dryrun_cancelled[{offer.order_hash}]",
                        exc,
                    )
                else:
                    if marked:
                        local_cancelled += 1
                continue
            if offer.order_hash in successful_live_cancels:
                try:
                    state.mark_offer_status(
                        order_hash=offer.order_hash,
                        status="cancelled",
                    )
                except Exception as exc:
                    note_state_persistence_failure(
                        f"mark_cancelled[{offer.order_hash}]",
                        exc,
                    )
                continue
            if offer.order_hash in failed_hash_set:
                try:
                    state.mark_offer_status(
                        order_hash=offer.order_hash,
                        status="killswitch_failed",
                    )
                except Exception as exc:
                    note_state_persistence_failure(
                        f"mark_killswitch_failed[{offer.order_hash}]",
                        exc,
                    )
                continue
            if not exchange_lookup_failed:
                try:
                    marked = state.mark_offer_status(
                        order_hash=offer.order_hash,
                        status="cancelled",
                    )
                except Exception as exc:
                    note_state_persistence_failure(
                        f"mark_already_gone[{offer.order_hash}]",
                        exc,
                    )
                else:
                    if marked:
                        already_gone += 1

        active_hash_set = {offer.order_hash for offer in okx_active_offers}
        for order_hash in failed_order_hashes:
            if order_hash in active_hash_set:
                continue
            try:
                state.upsert_active_offer(
                    order_hash=order_hash,
                    collection=exchange_order_collections.get(order_hash, "exchange_unknown"),
                    chain=chain,
                    price_bnb=0.0,
                    status="killswitch_failed",
                )
            except Exception as exc:
                note_state_persistence_failure(
                    f"upsert_killswitch_failed[{order_hash}]",
                    exc,
                )

        for quarantine_id, collection in unidentified_exchange_orders.items():
            try:
                state.upsert_active_offer(
                    order_hash=quarantine_id,
                    collection=collection,
                    chain=chain,
                    price_bnb=0.0,
                    status="killswitch_failed",
                )
            except Exception as exc:
                note_state_persistence_failure(
                    f"upsert_unidentified[{quarantine_id}]",
                    exc,
                )

    local_state_persistence_error = "; ".join(local_state_persistence_errors) or None
    local_state_persistence_failed = bool(local_state_persistence_errors)

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
        fatal_error=fatal_error,
        local_state_lookup_failed=local_state_lookup_failed,
        local_state_lookup_error=local_state_lookup_error,
        local_state_persistence_failed=local_state_persistence_failed,
        local_state_persistence_error=local_state_persistence_error,
    )
    if state is None:
        if state_unavailable_error:
            logger.warning(
                "Kill switch chain=%s completed without local state persistence: %s",
                chain,
                state_unavailable_error,
            )
        return result
    if local_state_lookup_failed or local_state_persistence_failed:
        _record_chain_audit_best_effort(state, result)
    else:
        _record_chain_audit(state, result)
    return result


def _record_chain_audit(state: PositionState, result: KillSwitchChainResult) -> None:
    persistence_suffix = (
        ";local_state_persistence_failed=1"
        if result.local_state_persistence_failed
        else ""
    )
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
            f"local_state_lookup_failed={1 if result.local_state_lookup_failed else 0};"
            f"exchange_lookup_failed={1 if result.exchange_lookup_failed else 0};"
            f"fatal={1 if result.fatal_error else 0}{persistence_suffix}"
        ),
    )
    errors = list(result.failed)
    if result.local_state_lookup_failed:
        errors.append(f"local_state_lookup:{result.local_state_lookup_error}")
    if result.local_state_persistence_failed:
        errors.append(f"local_state_persistence:{result.local_state_persistence_error}")
    if result.fatal_error:
        errors.append(f"fatal:{result.fatal_error}")
    payload = {
        "exchange_seen": result.exchange_seen,
        "live_cancelled": result.live_cancelled,
        "local_cancelled": result.local_cancelled,
        "already_gone": result.already_gone,
        "local_state_lookup_failed": result.local_state_lookup_failed,
        "local_state_lookup_error": result.local_state_lookup_error,
        "exchange_lookup_failed": result.exchange_lookup_failed,
        "exchange_lookup_error": result.exchange_lookup_error,
        "failed": list(result.failed),
        "fatal_error": result.fatal_error,
    }
    if result.local_state_persistence_failed:
        payload["local_state_persistence_failed"] = True
        payload["local_state_persistence_error"] = result.local_state_persistence_error
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
        payload=payload,
    )


def _record_chain_audit_best_effort(
    state: PositionState,
    result: KillSwitchChainResult,
) -> None:
    try:
        _record_chain_audit(state, result)
    except Exception:
        logger.exception(
            "Kill switch audit persistence failed chain=%s; continuing remaining chains",
            result.chain,
        )


def format_killswitch_result(result: KillSwitchResult) -> str:
    lines = [
        "killswitch_activated",
        "dry_run=true",
        f"chains={','.join(item.chain for item in result.chains)}",
    ]
    if result.preflight_error:
        lines.append(f"preflight_error={result.preflight_error}")
    for item in result.chains:
        has_effect_detail = bool(
            item.active_offers_seen
            or item.exchange_seen
            or item.live_cancelled
            or item.local_cancelled
            or item.already_gone
            or item.failed
            or item.local_state_lookup_failed
            or item.local_state_persistence_failed
        )
        if item.fatal_error and not has_effect_detail:
            lines.append(f"chain={item.chain} fatal_error={item.fatal_error}")
            continue
        persistence_suffix = (
            " state_persist_failed=1"
            if item.local_state_persistence_failed
            else ""
        )
        exchange_suffix = (
            " exchange_lookup_failed=1"
            if item.exchange_lookup_failed
            else ""
        )
        fatal_suffix = (
            f" fatal_error={item.fatal_error}"
            if item.fatal_error
            else ""
        )
        lines.append(
            f"chain={item.chain} active_offers_seen={item.active_offers_seen} "
            f"exchange_seen={item.exchange_seen} live_cancelled={item.live_cancelled} "
            f"local_cancelled={item.local_cancelled} already_gone={item.already_gone} "
            f"failed={len(item.failed)} zombies={len(item.failed)} "
            f"state_lookup_failed={1 if item.local_state_lookup_failed else 0}"
            f"{persistence_suffix}{exchange_suffix}{fatal_suffix}"
        )
    lines.append(f"total_failed={result.total_failed}")
    return "\n".join(lines)