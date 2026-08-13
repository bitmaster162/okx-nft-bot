from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from okx_nft_bot.config import Settings, get_rpc_urls, validate_execution_chain
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

if TYPE_CHECKING:
    from okx_nft_bot.undercutter.state import PositionState


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _primary_rpc_url(chain: str) -> str:
    """Back-compat accessor: first RPC URL from the failover list."""
    urls = get_rpc_urls(chain)
    if not urls:
        raise RuntimeError(f"No RPC URL configured for chain {chain!r}")
    return urls[0]


MIN_OFFER_PRICE_USD = _env_float("MIN_OFFER_PRICE_USD", 0.001)


@dataclass(slots=True)
class ReconciliationResult:
    chain: str
    exchange_seen: int
    local_active_seen: int
    local_dry_run_ignored: int
    local_marked_missing: int
    local_refreshed: int
    local_added_from_exchange: int
    malformed_exchange_rows: int
    exchange_missing_order_hashes: list[str]
    imported_order_hashes: list[str]
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _ExchangeOffer:
    order_hash: str
    collection: str | None
    chain: str
    price_bnb: float | None


class ExecutionGovernor:
    def __init__(
        self,
        *,
        settings: Settings,
        state: PositionState | None = None,
        api_client: OKXAPIClient | None = None,
    ) -> None:
        if state is None:
            from okx_nft_bot.undercutter.state import PositionState

            state = PositionState(settings.execution_db_path)
        self.settings = settings
        self.state = state
        self.api_client = api_client or OKXAPIClient(settings=settings)
        self.state.audit_integrity()

    def effective_dry_run(self, configured_dry_run: bool | None = None) -> bool:
        resolved = self.settings.dry_run if configured_dry_run is None else bool(configured_dry_run)
        return self.state.effective_dry_run(resolved)

    def get_live_arm_state(self, *, now: datetime | None = None) -> dict[str, Any]:
        return self.state.get_live_arm_state(now=now)

    def audit_state(self) -> dict[str, Any]:
        return self.state.audit_integrity().to_dict()

    def get_rate_limit_snapshot(
        self,
        *,
        chain: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self.state.get_rate_limit_snapshot(
            now=(now or datetime.now(timezone.utc)),
            max_live_offers_per_hour=self.settings.max_live_offers_per_hour,
            max_bnb_per_day=self.settings.max_bnb_per_day,
            submit_cooldown_seconds=self.settings.submit_cooldown_seconds,
        )

    def check_live_submit_allowed(
        self,
        *,
        action_type: str,
        collection: str,
        chain: str,
        price_bnb: float,
        price_usd: float | None = None,
        configured_dry_run: bool | None = None,
    ) -> str | None:
        """Gate a live submission through all rate/arm/cap checks.

        CRITICAL INVARIANT — `price_bnb` must be denominated in **BNB-equivalent**,
        never a raw token amount in another currency. The daily_bnb accumulator
        compares it against `settings.max_bnb_per_day` (default 5 BNB ~ $3000).

        If your offer is in USDT/USDC/WETH/etc, normalize via USD before calling:
            price_bnb_equiv = (price_usd / bnb_usd_price)

        Otherwise a 10 USDT offer is treated as 10 BNB (~$6000) and gets blocked,
        which is exactly the regression that hit `counter_bidder._submit_bsc`
        on 2026-04-21 (see _normalize_to_bnb helper).
        """
        _ = action_type, collection
        if self.effective_dry_run(configured_dry_run):
            return "dry_run_enabled"
        if price_usd is not None:
            ok, reason = self.check_min_price(price_usd)
            if not ok:
                return f"min_price_guard: {reason}"
        # Block new submissions if killswitch left failed (zombie) offers
        ks_failed = self.state.get_killswitch_failed_offers(chain=chain)
        if ks_failed:
            return (
                f"killswitch_failed: {len(ks_failed)} zombie offer(s) need manual cancel "
                "before new submissions are allowed"
            )
        # Block if killswitch is active with force_dry_run
        if self.state.is_force_dry_run():
            return "killswitch/integrity: force_dry_run is set — use /armlive to re-enable"
        arm_state = self.get_live_arm_state()
        if not arm_state["armed"]:
            if arm_state.get("expires_at"):
                return "live arm expired"
            return "live arm required"
        # Always use fresh timestamp to avoid stale snapshot with rapid sequential calls
        now = datetime.now(timezone.utc)
        snapshot = self.get_rate_limit_snapshot(chain=chain, now=now)
        if snapshot["hourly_count"] >= self.settings.max_live_offers_per_hour:
            return (
                f"rate limit hit: {snapshot['hourly_count']}/{self.settings.max_live_offers_per_hour} "
                "live submits in the last hour"
            )
        if snapshot["daily_bnb"] + price_bnb > self.settings.max_bnb_per_day:
            return (
                f"daily BNB-equivalent cap hit: spent {snapshot['daily_bnb']:.4f} + "
                f"offer {price_bnb:.6f} = {snapshot['daily_bnb'] + price_bnb:.6f} BNB "
                f"> cap {self.settings.max_bnb_per_day:.4f} BNB"
            )
        if snapshot["cooldown_remaining_seconds"] > 0:
            return f"submit cooldown active: {snapshot['cooldown_remaining_seconds']}s remaining"
        return None

    def allocate_seaport_counter(
        self,
        wallet: str,
        chain: str,
        *,
        resync_after_seconds: int = 60,
    ) -> int:
        """Atomic Seaport counter allocation.

        Returns the counter value the caller should use for the next signed
        order. Persists the local counter in ``seaport_counter`` (execution.sqlite3)
        so multiple engines on the same wallet do not race. On first call, or
        when the local record is older than ``resync_after_seconds``, the value
        is re-fetched from chain via Seaport ``getCounter``. The counter is
        NOT incremented per order — see state.allocate_seaport_counter docstring.
        """
        from okx_nft_bot.signing.seaport_signer import get_counter

        def _fetch(w: str, c: str) -> int:
            return int(get_counter(w, rpc_urls=get_rpc_urls(c)))

        return self.state.allocate_seaport_counter(
            wallet=wallet,
            chain=chain,
            fetch_onchain_fn=_fetch,
            resync_after_seconds=resync_after_seconds,
        )

    def allocate_nonce(
        self,
        wallet: str,
        chain: str,
        *,
        resync_after_seconds: int = 60,
    ) -> int:
        """Atomic transaction nonce allocation.

        Serializes nonce assignment across processes on the same wallet+chain
        via SQLite BEGIN IMMEDIATE (see PositionState.allocate_nonce). The
        on-chain probe uses the ``pending`` block tag so that queued
        transactions from other tools are respected.
        """
        from web3 import Web3

        def _fetch(w: str, c: str) -> int:
            urls = get_rpc_urls(c)
            last_err: Exception | None = None
            for url in urls:
                try:
                    w3 = Web3(Web3.HTTPProvider(url))
                    return int(w3.eth.get_transaction_count(Web3.to_checksum_address(w), "pending"))
                except Exception as exc:
                    last_err = exc
                    continue
            raise RuntimeError(f"All {len(urls)} RPC endpoints failed for chain {c}: {last_err}")

        return self.state.allocate_nonce(
            wallet=wallet,
            chain=chain,
            fetch_onchain_fn=_fetch,
            resync_after_seconds=resync_after_seconds,
        )

    def check_min_price(self, price_usd: float) -> tuple[bool, str]:
        """Global floor guard for any live offer submission.

        Returns (allowed, reason). ``reason`` is empty on success.
        """
        floor = MIN_OFFER_PRICE_USD
        if price_usd is None or price_usd < floor:
            return False, f"price ${float(price_usd or 0.0):.4f} < min ${floor:.4f}"
        return True, ""

    def reconcile_active_offers(self, *, chain: str | None = None) -> ReconciliationResult:
        resolved_chain = validate_execution_chain(chain or self.settings.execution_chain)

        exchange_offers_raw = self.api_client.get_my_offers(
            chain=resolved_chain,
            require_all_endpoints=True,
        )
        exchange_offers: list[_ExchangeOffer] = []
        malformed_exchange_rows = 0
        for row in exchange_offers_raw:
            normalized = self._normalize_exchange_offer(row, chain=resolved_chain)
            if normalized is None:
                malformed_exchange_rows += 1
                continue
            exchange_offers.append(normalized)

        exchange_by_hash = {offer.order_hash: offer for offer in exchange_offers}
        # Absence is authoritative only when every exchange row had a usable
        # identity. A malformed/unidentifiable row may be the local live order
        # we cannot match, so keep local exposure active until a clean snapshot
        # can prove that order is absent.
        negative_inference_allowed = malformed_exchange_rows == 0
        local_active = self.state.get_active_offers(chain=resolved_chain)
        local_dry_run_ignored = sum(1 for offer in local_active if offer.order_hash.startswith("dryrun-"))
        local_live = [offer for offer in local_active if not offer.order_hash.startswith("dryrun-")]
        local_live_by_hash = {offer.order_hash: offer for offer in local_live}

        local_marked_missing = 0
        local_refreshed = 0
        exchange_missing_order_hashes: list[str] = []
        for offer in local_live:
            if offer.order_hash not in exchange_by_hash:
                if negative_inference_allowed and self.state.mark_offer_status(
                    order_hash=offer.order_hash,
                    status="exchange_missing",
                ):
                    local_marked_missing += 1
                    exchange_missing_order_hashes.append(offer.order_hash)
                continue
            exchange_offer = exchange_by_hash[offer.order_hash]
            refreshed_price = exchange_offer.price_bnb if exchange_offer.price_bnb is not None else offer.price_bnb
            refreshed_collection = exchange_offer.collection or offer.collection
            self.state.upsert_active_offer(
                order_hash=offer.order_hash,
                collection=refreshed_collection,
                chain=resolved_chain,
                price_bnb=refreshed_price,
                status="active",
                current_floor=exchange_offer.price_bnb,
                preview_payload=offer.preview_payload,
            )
            local_refreshed += 1

        local_added_from_exchange = 0
        imported_order_hashes: list[str] = []
        for offer in exchange_offers:
            if offer.order_hash in local_live_by_hash:
                continue
            if offer.collection is None or offer.price_bnb is None:
                malformed_exchange_rows += 1
                continue
            self.state.upsert_active_offer(
                order_hash=offer.order_hash,
                collection=offer.collection,
                chain=resolved_chain,
                price_bnb=offer.price_bnb,
                status="active",
                current_floor=offer.price_bnb,
            )
            local_added_from_exchange += 1
            imported_order_hashes.append(offer.order_hash)

        completed_at = datetime.now(timezone.utc).isoformat()
        self.state.set_runtime_value("last_reconcile_at", completed_at)
        self.state.set_runtime_value(f"last_reconcile_at_{resolved_chain}", completed_at)
        # `last_reconcile_chain` is a legacy BSC-only runtime key whose integrity
        # validator rejects `eth`. Multi-chain reconciliation already has
        # canonical per-chain timestamps above, so keep the incompatible legacy
        # key absent instead of writing a valid ETH reconciliation into quarantine.
        self.state.set_runtime_value("last_reconcile_chain", None)
        self.state.set_runtime_value("last_reconcile_exchange_seen", exchange_seen := len(exchange_offers))
        self.state.set_runtime_value("last_reconcile_local_active_seen", len(local_active))
        self.state.set_runtime_value("last_reconcile_local_marked_missing", local_marked_missing)
        self.state.set_runtime_value("last_reconcile_local_refreshed", local_refreshed)
        self.state.set_runtime_value("last_reconcile_local_added", local_added_from_exchange)
        self.state.set_runtime_value("last_reconcile_malformed_exchange_rows", malformed_exchange_rows)

        return ReconciliationResult(
            chain=resolved_chain,
            exchange_seen=exchange_seen,
            local_active_seen=len(local_active),
            local_dry_run_ignored=local_dry_run_ignored,
            local_marked_missing=local_marked_missing,
            local_refreshed=local_refreshed,
            local_added_from_exchange=local_added_from_exchange,
            malformed_exchange_rows=malformed_exchange_rows,
            exchange_missing_order_hashes=exchange_missing_order_hashes,
            imported_order_hashes=imported_order_hashes,
            completed_at=completed_at,
        )

    @classmethod
    def _normalize_exchange_offer(cls, payload: dict[str, Any], *, chain: str) -> _ExchangeOffer | None:
        order_hash = cls._first_text(payload, "offerId", "orderHash", "id")
        if not order_hash:
            return None
        collection = cls._first_text(
            payload,
            "contractAddress",
            "collectionAddress",
            "collection",
            "collection_address",
        )
        price_bnb = cls._coerce_price(payload.get("price"))
        return _ExchangeOffer(
            order_hash=order_hash,
            collection=collection.lower() if collection else None,
            chain=chain,
            price_bnb=price_bnb,
        )

    @staticmethod
    def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @classmethod
    def _coerce_price(cls, raw: Any, _depth: int = 0) -> float | None:
        if raw is None or _depth > 4:
            return None
        if isinstance(raw, dict):
            for key in ("amount", "native", "value", "price"):
                candidate = cls._coerce_price(raw.get(key), _depth + 1)
                if candidate is not None:
                    return candidate
            return None
        try:
            price = float(raw)
        except (ValueError, TypeError):
            return None
        if price < 0:
            return None
        # OKX /get_my_offers returns price as a wei-denominated string
        # (e.g. "111655499999999990" == 0.11166 native units). Legacy
        # callers of this helper passed already-decimal floats; apply a
        # threshold heuristic so both shapes are coerced to native units.
        # Any real-world NFT offer denominated in wei exceeds 1e10 (even
        # 1e-8 BNB = 1e10 wei); any ether-denominated offer stays well
        # below 1e10 (1e10 BNB is economically impossible).
        if price >= 1e10:
            price = price / 1e18
        return price
