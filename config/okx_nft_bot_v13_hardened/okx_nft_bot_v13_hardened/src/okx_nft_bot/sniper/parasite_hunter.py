"""
Parasite Hunter v4 — unified WL capture + parasite undercut + missclick buy.

Strategy (3 phases per scan cycle):
  PHASE 1 — WL CAPTURE (highest priority):
    Scan ALL offers on Binance WL collections.
    Undercut EVERYONE except self/friends.
    Goal: be the best offer on every WL collection.

  PHASE 2 — PARASITE NON-WL HUNT:
    Scan 37 parasite wallets, find their offers on NON-WL collections.
    Undercut if offer < $0.01 or collection has config in buy_config.
    Place up to ~10 offers per collection.

  PHASE 3 — MISSCLICK BUY:
    Check WL listings — buy anything priced below our max ($0.51 default).

Env vars:
    PARASITE_HUNTER_ENABLED=1
    PARASITE_HUNTER_DRY_RUN=1
    PARASITE_HUNTER_MAX_PER_COLLECTION=50
    PARASITE_HUNTER_UNDERCUT_BPS=50       # undercut by 0.5%
    PARASITE_HUNTER_MAX_USD=0.51          # max offer value in USD
    PARASITE_HUNTER_DELAY_SECONDS=1.0     # delay between offers
    PARASITE_HUNTER_SCAN_INTERVAL=300     # full scan every 5 min
    PARASITE_HUNTER_COLLECTION_DELAY=2.0  # delay between collections during scan
    PARASITE_HUNTER_CHAINS=bsc,eth        # chains to scan
    PARASITE_HUNTER_OFFER_CURRENCIES=WBNB,USDT  # currencies we can offer in
    PARASITE_HUNTER_NONWL_MAX_USD=0.01    # max for non-WL parasite offers
    PARASITE_HUNTER_NONWL_QTY=10          # offers per non-WL collection
    FRIEND_WALLETS=0x...,0x...            # friend wallets — never undercut
    BUYER_WALLET_ADDRESS=0x...            # our wallet — never undercut
    OKX_SLUG_MAP=0xaddr1=slug1,0xaddr2=slug2  # contract → OKX page slug
"""
from __future__ import annotations

import json as _json
import logging
import os
import re as _re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("sniper.parasite_hunter")


# ── Live price fetcher ────────────────────────────────────────

class PriceFetcher:
    """Fetch live crypto prices from Binance public API. Cached for 60s."""

    BINANCE_API = "https://api.binance.com/api/v3/ticker/price"
    SYMBOLS = {
        "BNBUSDT": ("BNB", "WBNB"),
        "ETHUSDT": ("ETH", "WETH"),
    }
    # Stablecoins → always $1
    STABLES = {"USDT", "USDC", "BUSD", "DAI", "TUSD"}
    CACHE_TTL = 60  # seconds

    def __init__(self):
        self._cache: dict[str, float] = {}  # symbol → USD price
        self._cache_ts: float = 0

    def get_usd_price(self, currency: str) -> float:
        """Return USD price for a currency symbol. Returns 0 if unknown."""
        cur = currency.upper().strip()
        if cur in self.STABLES:
            return 1.0
        if cur == "USD":
            return 1.0

        self._refresh_if_stale()

        return self._cache.get(cur, 0.0)

    def convert(self, amount: float, from_cur: str, to_cur: str) -> float:
        """Convert amount from one currency to another via USD."""
        if from_cur.upper() == to_cur.upper():
            return amount
        from_usd = self.get_usd_price(from_cur)
        to_usd = self.get_usd_price(to_cur)
        if from_usd <= 0 or to_usd <= 0:
            return 0.0
        return amount * from_usd / to_usd

    def to_usd(self, amount: float, currency: str) -> float:
        """Convert amount to USD."""
        price = self.get_usd_price(currency)
        return amount * price if price > 0 else 0.0

    def from_usd(self, usd_amount: float, currency: str) -> float:
        """Convert USD amount to currency."""
        price = self.get_usd_price(currency)
        return usd_amount / price if price > 0 else 0.0

    def _refresh_if_stale(self):
        if time.time() - self._cache_ts < self.CACHE_TTL:
            return
        try:
            from curl_cffi import requests as http
        except ImportError:
            import requests as http  # type: ignore

        try:
            resp = http.get(self.BINANCE_API, timeout=5)
            data = resp.json()
            new_cache: dict[str, float] = {}
            for item in data:
                sym = item.get("symbol", "")
                if sym in self.SYMBOLS:
                    price = float(item["price"])
                    for alias in self.SYMBOLS[sym]:
                        new_cache[alias] = price
            self._cache = new_cache
            self._cache_ts = time.time()
            log.debug("💰 Prices updated: %s",
                      {k: f"${v:.2f}" for k, v in new_cache.items()})
        except Exception as exc:
            log.warning("Price fetch failed: %s", exc)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return float(v)


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return int(v)


@dataclass
class ParasiteOffer:
    """Single offer parsed from API."""
    collection_address: str
    collection_name: str
    chain: str
    token_id: str
    price: float
    currency: str
    offer_id: str
    source_type: str  # "token_offer" or "collection_offer"
    maker: str = ""   # wallet address of offer maker
    expires_at: str = ""


@dataclass
class HuntResult:
    collection_address: str
    collection_name: str
    chain: str
    parasite_offers_found: int
    our_offers_placed: int
    our_offers_failed: int
    our_offers_skipped: int


@dataclass
class ScanReport:
    """Summary of a full scan cycle (all 3 phases)."""
    chains_scanned: list[str] = field(default_factory=list)
    # Phase 1: WL capture
    wl_offers_found: int = 0
    wl_collections: int = 0
    wl_undercuts_placed: int = 0
    wl_already_best: int = 0
    wl_friend_skipped: int = 0
    # Phase 2: Parasite non-WL
    nonwl_offers_found: int = 0
    nonwl_collections: int = 0
    nonwl_undercuts_placed: int = 0
    # Phase 3: Missclick buy (WL)
    buy_attempts: int = 0
    buy_success: int = 0
    buy_dry_run: int = 0
    buy_skipped: int = 0
    # Phase 3c: Missclick buy (non-WL from buy_config)
    cfg_buy_attempts: int = 0
    cfg_buy_success: int = 0
    cfg_buy_dry_run: int = 0
    cfg_buy_skipped: int = 0
    # Legacy compat
    total_offers_found: int = 0
    collections_with_offers: int = 0
    by_collection: dict[str, list[ParasiteOffer]] = field(default_factory=dict)
    undercuts_placed: int = 0
    undercuts_failed: int = 0
    undercuts_skipped: int = 0
    scan_duration_sec: float = 0.0


class ParasiteHunter:
    """Unified WL capture + parasite undercut system for OKX.

    v4: Phase 1 scans ALL offers on WL collections, Phase 2 hunts parasites on non-WL.
    Protected wallets (self + friends) are never undercut.
    """

    def __init__(self, binance_whitelist: dict[str, dict], buy_config: dict):
        self.enabled = _env_bool("PARASITE_HUNTER_ENABLED", False)
        self.dry_run = _env_bool("PARASITE_HUNTER_DRY_RUN", True)

        # ── Parasite wallets (targets to undercut) ──
        self.target_wallets: set[str] = set()
        pw_raw = _env("PARASITE_WALLETS")
        if pw_raw:
            self.target_wallets = {w.strip().lower() for w in pw_raw.split(",") if w.strip()}
        explicit = _env("PARASITE_HUNTER_TARGET")
        if explicit:
            self.target_wallets.add(explicit.strip().lower())
        if not self.target_wallets:
            self.target_wallets = {"0xf1771cf8831393422189330a79dd896223c357a4"}

        # ── Our wallet + friend wallets (NEVER undercut these) ──
        self.our_wallet = _env("BUYER_WALLET_ADDRESS", "").strip().lower()
        self.friend_wallets: set[str] = set()
        fw_raw = _env("FRIEND_WALLETS")
        if fw_raw:
            self.friend_wallets = {w.strip().lower() for w in fw_raw.split(",") if w.strip()}
        # Protected set = our wallet + all friends
        self.protected_wallets: set[str] = set(self.friend_wallets)
        if self.our_wallet:
            self.protected_wallets.add(self.our_wallet)

        # ── WL capture settings ──
        self.max_per_collection = _env_int("PARASITE_HUNTER_MAX_PER_COLLECTION", 50)
        self.undercut_bps = _env_int("PARASITE_HUNTER_UNDERCUT_BPS", 50)
        self.max_usd = _env_float("PARASITE_HUNTER_MAX_USD", 0.51)
        self.delay = _env_float("PARASITE_HUNTER_DELAY_SECONDS", 1.0)
        self.scan_interval = _env_int("PARASITE_HUNTER_SCAN_INTERVAL", 300)
        self.collection_delay = _env_float("PARASITE_HUNTER_COLLECTION_DELAY", 2.0)
        self.chains = [
            c.strip().lower()
            for c in _env("PARASITE_HUNTER_CHAINS", "bsc").split(",")
            if c.strip()
        ]
        self.offer_currencies = [
            c.strip().upper()
            for c in _env("PARASITE_HUNTER_OFFER_CURRENCIES", "WBNB,WETH,USDT").split(",")
            if c.strip()
        ]

        # ── Phase 2: non-WL parasite hunt settings ──
        self.nonwl_max_usd = _env_float("PARASITE_HUNTER_NONWL_MAX_USD", 0.001)
        self.nonwl_qty = _env_int("PARASITE_HUNTER_NONWL_QTY", 10)

        self.binance_whitelist = binance_whitelist
        self.buy_config = buy_config
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Live price fetcher
        self.prices = PriceFetcher()

        # Live state — updated after each scan
        self.last_report: ScanReport | None = None
        self.total_scans = 0
        self.already_winning = 0
        self._friend_skipped_count = 0

        # Telegram
        self.tg_bot_token = _env("TELEGRAM_BOT_TOKEN")
        self.tg_chat_id = _env("TELEGRAM_CHAT_ID")

        # OKX API client (lazy init)
        self._okx_client = None
        self._settings_cache = None
        self._execution_state = None
        self._execution_governor = None

        # OpenSea client (for ETH collection offers)
        self.opensea_enabled = False
        self.opensea_client = None
        opensea_api_key = _env("OPENSEA_API_KEY")
        if opensea_api_key:
            try:
                from okx_nft_bot.clients.opensea import OpenSeaClient
                from okx_nft_bot.config import load_settings
                settings = load_settings()
                self.opensea_client = OpenSeaClient(settings=settings)
                self.opensea_enabled = True
            except Exception as exc:
                log.warning("OpenSeaClient init failed: %s", exc)
                self.opensea_enabled = False
                self.opensea_client = None

        # Anti-stacking cooldown: collection → timestamp of last placed offer
        # If we placed an offer < COOLDOWN seconds ago, skip to prevent duplicates
        self._last_placed: dict[str, float] = {}
        self._PLACE_COOLDOWN = 600  # 10 minutes

        # Local offer tracker: "addr:chain" → list of offer_ids we placed
        # Backup for self-bidding protection when OKX API fails to return our offers
        self._local_placed_offers: dict[str, list[str]] = {}

        # ── OKX page slug map (address → slug) for new API endpoints ──
        # Old priapi/v5/nft/ec/ endpoints are DEAD (404).
        # New approach: parse collection page HTML by slug to get nftId + bestOffer,
        # then use priapi/v1/nft/order/offers?nftId=X for full offer list.
        self._slug_map: dict[str, str] = {}
        slug_raw = _env("OKX_SLUG_MAP")
        if slug_raw:
            for pair in slug_raw.split(","):
                pair = pair.strip()
                if "=" in pair:
                    addr, slug = pair.split("=", 1)
                    self._slug_map[addr.strip().lower()] = slug.strip()
        # Cache: address → {"nft_id": str, "best_offer": dict, "ts": float}
        self._collection_page_cache: dict[str, dict] = {}
        self._COLLECTION_PAGE_TTL = 120  # seconds — refresh every 2 min
        self._best_offer_cache: dict[str, dict] = {}
        # Per-scan cache for offers fetched by nftId — avoids N+1 queries
        # when _fetch_best_offer_maker, _find_our_offer, _find_all_our_offers
        # all query the same nftId in one scan cycle
        self._nftid_offers_cache: dict[str, list[dict]] = {}

        # WL lookup index (addr -> info) for fast name resolution
        self._wl_index: dict[str, dict] = {
            k.lower(): v for k, v in self.binance_whitelist.items()
        }
        # Reverse index: name.lower() → addr for fast alert lookup (#10)
        self._wl_name_to_addr: dict[str, str] = {}
        for addr, info in self._wl_index.items():
            name = (info.get("collection_name") or info.get("name") or "").lower()
            if name:
                self._wl_name_to_addr[name] = addr

        status = "ENABLED" if self.enabled else "DISABLED"
        mode = "DRY RUN" if self.dry_run else "LIVE"
        log.info(
            "ParasiteHunter v4: %s (%s) targets=%d friends=%d undercut=%dbps "
            "max=$%.2f chains=%s wl=%d currencies=%s",
            status, mode, len(self.target_wallets), len(self.friend_wallets),
            self.undercut_bps, self.max_usd, ",".join(self.chains),
            len(self.binance_whitelist),
            ",".join(self.offer_currencies)
        )
        log.info(
            "📣 OpenSea offers: %s (ETH only)",
            "ENABLED" if self.opensea_enabled else "DISABLED (no API key)"
        )
        # ── Phase 3: Missclick buy ──
        self._buyer = None
        self._buy_cooldowns: dict[str, float] = {}  # collection → last buy timestamp
        self._cycle_buys = 0  # buys this scan cycle
        buy_settings = self.buy_config.get("buy_settings", {})
        self._buy_enabled = buy_settings.get("enabled", False)
        self._max_buys_per_collection = buy_settings.get("max_buys_per_collection", 2)
        self._max_buys_per_cycle = buy_settings.get("max_total_buys_per_cycle", 5)
        self._buy_cooldown_seconds = buy_settings.get("cooldown_seconds", 60)
        # Phase 3c: buy non-WL collections from buy_config (separate dry_run)
        self._cfg_buy_dry_run = _env("AUTO_BUY_CONFIG_DRY_RUN") in ("1", "true", "yes", "on", "")
        if not _env("AUTO_BUY_CONFIG_DRY_RUN"):
            self._cfg_buy_dry_run = True  # default: dry run for safety
        # Build set of WL addresses for exclusion in Phase 3c
        self._wl_addrs_set = set(self._wl_index.keys())

        if self._buy_enabled:
            try:
                from okx_nft_bot.sniper.buyer import OKXInstantBuyer
                self._buyer = OKXInstantBuyer()
                log.info("🛒 Missclick Buy: ENABLED (max %d/cycle, %d/collection, cooldown %ds)",
                         self._max_buys_per_cycle, self._max_buys_per_collection,
                         self._buy_cooldown_seconds)
                log.info("🛒 Phase 3c (non-WL config buy): dry_run=%s", self._cfg_buy_dry_run)
            except Exception as exc:
                log.error("OKXInstantBuyer init failed: %s — Phase 3 disabled", exc)
                self._buy_enabled = False
        else:
            log.info("🛒 Missclick Buy: DISABLED (buy_settings.enabled=false)")

        # Log protected wallets for debugging self-bidding issues
        log.info(
            "🛡 Protected wallets (%d): our=%s friends=%s",
            len(self.protected_wallets),
            self.our_wallet[:14] + "..." if self.our_wallet else "NOT SET",
            [w[:14] + "..." for w in self.friend_wallets] if self.friend_wallets else "none"
        )

    # ── OKX API client ─────────────────────────────────────────

    def _get_okx_client(self):
        """Lazy-init OKX API client using existing infrastructure."""
        if self._okx_client is not None:
            return self._okx_client
        try:
            from okx_nft_bot.config import load_settings
            from okx_nft_bot.counterbid.okx_api import OKXAPIClient
            settings = load_settings()
            self._okx_client = OKXAPIClient(settings=settings)
            return self._okx_client
        except Exception as exc:
            log.warning("OKXAPIClient init failed: %s", exc)
            return None

    def _get_provider(self):
        """Get or create OKXOffersProvider for wallet-level queries."""
        client = self._get_okx_client()
        if client and client.provider:
            return client.provider
        if client:
            return client._provider()
        return None

    def _get_settings(self):
        if self._settings_cache is not None:
            return self._settings_cache
        from okx_nft_bot.config import load_settings

        self._settings_cache = load_settings()
        return self._settings_cache

    def _get_execution_state(self):
        if self._execution_state is not None:
            return self._execution_state
        from okx_nft_bot.undercutter.state import PositionState

        settings = self._get_settings()
        self._execution_state = PositionState(settings.execution_db_path)
        return self._execution_state

    def _get_execution_governor(self):
        if self._execution_governor is not None:
            return self._execution_governor
        from okx_nft_bot.execution_governor import ExecutionGovernor

        settings = self._get_settings()
        self._execution_governor = ExecutionGovernor(
            settings=settings,
            state=self._get_execution_state(),
            api_client=self._get_okx_client(),
        )
        return self._execution_governor

    def _record_execution_submit_event(
        self,
        *,
        chain: str,
        collection: str,
        price_bnb: float | None,
        status: str,
        reason: str,
    ) -> None:
        try:
            state = self._get_execution_state()
            state.record_submit_event(
                engine="parasite_hunter",
                action_type="LIVE_PARASITE_HUNTER",
                collection=collection,
                chain=chain,
                price_bnb=price_bnb,
                status=status,
                reason=reason,
            )
        except Exception as exc:
            log.debug("parasite_hunter submit event skipped: %s", exc)

    # ── Background scanner ─────────────────────────────────────

    def start_background_scan(self):
        """Start background thread that periodically runs wallet-level scan."""
        if not self.enabled:
            return
        if self._bg_thread and self._bg_thread.is_alive():
            return

        self._stop_event.clear()
        self._bg_thread = threading.Thread(
            target=self._background_loop, daemon=True, name="parasite-hunter"
        )
        self._bg_thread.start()
        log.info(
            "🎯 ParasiteHunter v4 background scan started "
            "(interval=%ds, chains=%s, targets=%d, friends=%d)",
            self.scan_interval, ",".join(self.chains),
            len(self.target_wallets), len(self.friend_wallets)
        )

    def stop_background_scan(self):
        """Stop background scanner."""
        self._stop_event.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=10)

    def _background_loop(self):
        """Main background loop — wallet-level scan each interval."""
        while not self._stop_event.is_set():
            try:
                report = self.scan_wallet()
                self.last_report = report
                self.total_scans += 1
                self._alert_scan_report(report)
            except Exception as exc:
                log.error("🎯 Background scan error: %s", exc)

            self._stop_event.wait(self.scan_interval)

    # ── WALLET-LEVEL SCAN (the main feature) ──────────────────

    def scan_wallet(self) -> ScanReport:
        """Multi-phase scan cycle:
        Phase 0: PRIORITY KILL — outbid ALL offers from priority parasite FIRST
        Phase 1: WL capture — undercut ALL offers on WL collections (except self/friends)
        Phase 2: Parasite non-WL — undercut parasite offers on non-WL if cheap or configured
        Phase 2b: Sell side — list our NFTs cheapest but above low_price
        Phase 3: Missclick buy — buy underpriced WL listings
        Phase 3c: Config buy — buy underpriced non-WL from buy_config
        """
        t0 = time.time()
        report = ScanReport()
        self.already_winning = 0
        self._friend_skipped_count = 0
        self._best_offer_cache = {}  # reset per-scan cache
        self._nftid_offers_cache = {}  # reset per-scan nftId offers cache
        self._processed_collections: set[str] = set()  # Phase dedup: prevent double-processing

        # ── Evict stale cache entries to prevent memory leaks ──
        now = time.time()
        # Purge cooldowns older than 2x cooldown period
        stale_cutoff = now - (self._PLACE_COOLDOWN * 2)
        self._last_placed = {k: v for k, v in self._last_placed.items()
                             if v > stale_cutoff}
        # Purge stale page cache entries (>5 min old)
        self._collection_page_cache = {
            k: v for k, v in self._collection_page_cache.items()
            if now - v.get("ts", 0) < 300
        }
        # Purge floor cache
        if hasattr(self, "_floor_cache"):
            self._floor_cache = {}

        # Pre-fetch prices
        self.prices._refresh_if_stale()
        bnb_usd = self.prices.get_usd_price("BNB")
        eth_usd = self.prices.get_usd_price("ETH")
        log.info("💰 Live prices: BNB=$%.2f ETH=$%.2f", bnb_usd, eth_usd)

        # ── Priority parasite wallet (Phase 0 target) ──
        priority_parasite = _env(
            "PRIORITY_PARASITE_WALLET",
            "0xf1771cf8831393422189330a79dd896223c357a4",
        ).strip().lower()

        for chain in self.chains:
            if self._stop_event.is_set():
                break
            report.chains_scanned.append(chain)

            # ══════════════════════════════════════════════
            # PHASE 0: PRIORITY PARASITE KILL
            # Fetch ALL offers from the priority parasite wallet and
            # outbid every single one BEFORE any other work.
            # This ensures the parasite gets crushed immediately each cycle.
            # ══════════════════════════════════════════════
            log.info("═══ PHASE 0: PRIORITY KILL %s…%s on %s ═══",
                     priority_parasite[:8], priority_parasite[-4:], chain.upper())
            try:
                p0_offers = self._fetch_single_wallet(priority_parasite, chain)
                log.info("🎯 PHASE 0 %s: fetched %d offers from priority parasite",
                         chain.upper(), len(p0_offers))

                if p0_offers:
                    # Group by collection
                    p0_by_coll: dict[str, list[ParasiteOffer]] = defaultdict(list)
                    for o in p0_offers:
                        p0_by_coll[o.collection_address].append(o)

                    log.info("🎯 PHASE 0 %s: %d collections to outbid",
                             chain.upper(), len(p0_by_coll))

                    for addr, coll_offers in p0_by_coll.items():
                        if self._stop_event.is_set():
                            break
                        name = coll_offers[0].collection_name or addr[:14]
                        log.info("  💀 PHASE 0: outbidding %d offers on %s",
                                 len(coll_offers), name)

                        result = self._undercut_collection(addr, coll_offers, chain)
                        self._processed_collections.add(f"{addr}:{chain}")

                        report.total_offers_found += len(coll_offers)
                        report.undercuts_placed += result.our_offers_placed
                        report.undercuts_failed += result.our_offers_failed
                        report.undercuts_skipped += result.our_offers_skipped

                        if self.collection_delay > 0 and not self.dry_run:
                            time.sleep(self.collection_delay)

                    log.info("✅ PHASE 0 %s: done — %d collections processed",
                             chain.upper(), len(p0_by_coll))

                    # Clear best-offer cache so Phase 1/2 don't use stale
                    # data from before our Phase 0 offers were placed.
                    self._best_offer_cache.clear()
                    self._nftid_offers_cache.clear()
                else:
                    log.info("✅ PHASE 0 %s: parasite has 0 offers — nothing to kill",
                             chain.upper())

            except Exception as exc:
                log.error("PHASE 0 PRIORITY KILL %s failed: %s",
                          chain.upper(), exc, exc_info=True)

            # ══════════════════════════════════════════════
            # PHASE 1: WL CAPTURE — be the best offer on every WL collection
            # Scans ALL offers (not just parasites) on each WL collection.
            # Undercuts anyone who isn't us or a friend.
            # ══════════════════════════════════════════════
            log.info("═══ PHASE 1: WL CAPTURE on %s ═══", chain.upper())

            # Get WL collection addresses for this chain
            # For BSC: skip collections not verified on OKX (saves ~82 wasted API calls)
            wl_addrs_chain = [
                addr for addr, info in self._wl_index.items()
                if info.get("network", "").lower() == chain
                and (chain != "bsc" or info.get("okx_verified", True))
            ]
            log.info("📋 %s: %d WL collections to scan", chain.upper(), len(wl_addrs_chain))

            wl_enemy_offers: list[ParasiteOffer] = []
            for i, coll_addr in enumerate(wl_addrs_chain):
                if self._stop_event.is_set():
                    break

                # Phase dedup: skip collections already processed in Phase 0
                dedup_key = f"{coll_addr}:{chain}"
                if dedup_key in self._processed_collections:
                    log.info("  ⏭ WL %s: already processed in Phase 0 — skip", coll_addr[:14])
                    continue

                top_offers = self._fetch_top_offers_for_collection(coll_addr, chain, limit=5)
                if not top_offers:
                    continue

                # Filter: keep only enemy offers (not us, not friends)
                # IMPORTANT: skip offers with empty maker — could be our own offer
                # that the API didn't return maker info for
                for o in top_offers:
                    if not o.maker:
                        log.warning("  ⚠ %s: offer with EMPTY maker (price=%.4f %s) — treating as unknown, skip to avoid self-bid",
                                    coll_addr[:14], o.price, o.currency)
                enemies = [o for o in top_offers if o.maker and o.maker not in self.protected_wallets]
                if not enemies:
                    # All top offers are ours/friends — we're winning
                    self.already_winning += 1
                    continue

                # Check: is our offer already #1?
                if top_offers[0].maker and top_offers[0].maker in self.protected_wallets:
                    self.already_winning += 1
                    continue

                wl_enemy_offers.extend(enemies)

                # Rate limit: don't hammer OKX
                if i % 20 == 19:
                    time.sleep(1.0)
                else:
                    time.sleep(0.15)

            # Group enemy offers by collection and undercut
            if wl_enemy_offers:
                by_wl: dict[str, list[ParasiteOffer]] = defaultdict(list)
                for o in wl_enemy_offers:
                    by_wl[o.collection_address].append(o)

                report.wl_offers_found += len(wl_enemy_offers)
                report.wl_collections = len(by_wl)

                for addr, coll_offers in by_wl.items():
                    report.by_collection[addr] = coll_offers
                    report.total_offers_found += len(coll_offers)

                    result = self._undercut_collection(addr, coll_offers, chain)
                    self._processed_collections.add(f"{addr}:{chain}")  # Phase dedup
                    report.undercuts_placed += result.our_offers_placed
                    report.wl_undercuts_placed += result.our_offers_placed
                    report.undercuts_failed += result.our_offers_failed
                    report.undercuts_skipped += result.our_offers_skipped
                    report.wl_friend_skipped += result.our_offers_skipped

                    if self.collection_delay > 0 and not self.dry_run:
                        time.sleep(self.collection_delay)

            report.wl_already_best = self.already_winning

            # ══════════════════════════════════════════════
            # PHASE 2: PARASITE NON-WL — cheap collections parasites sit on
            # Wallet-level scan of all parasites, then filter non-WL.
            # NOTE: BSC /offers endpoint ignores maker filter, but
            #       /collection-offers endpoint WORKS with maker on BSC.
            # ══════════════════════════════════════════════
            log.info("═══ PHASE 2: NON-WL PARASITE HUNT on %s ═══", chain.upper())

            all_parasite = self._fetch_wallet_offers(chain)
            nonwl_parasite = [o for o in all_parasite if o.collection_address not in self._wl_index]

            log.info("🕷 %s: %d parasite non-WL offers", chain.upper(), len(nonwl_parasite))

            if nonwl_parasite:
                by_nonwl: dict[str, list[ParasiteOffer]] = defaultdict(list)
                for o in nonwl_parasite:
                    by_nonwl[o.collection_address].append(o)

                report.nonwl_collections = len(by_nonwl)
                report.nonwl_offers_found += len(nonwl_parasite)

                for addr, coll_offers in by_nonwl.items():
                    has_config = addr in self.buy_config.get("collections", {})
                    cheapest_usd = min(
                        (self.prices.to_usd(o.price, o.currency) for o in coll_offers
                         if o.price > 0),
                        default=999
                    )
                    is_cheap = cheapest_usd <= self.nonwl_max_usd

                    if not has_config and not is_cheap:
                        log.debug("  ⏭ non-WL %s: skip (no config, min=$%.4f)",
                                  addr[:14], cheapest_usd)
                        continue

                    # Phase dedup: skip if already processed in Phase 1 (WL)
                    dedup_key = f"{addr}:{chain}"
                    if dedup_key in self._processed_collections:
                        log.info("  ⏭ non-WL %s: already processed in Phase 1 — skip", addr[:14])
                        continue

                    reason = "config" if has_config else f"cheap(${cheapest_usd:.4f})"
                    log.info("  🎯 non-WL %s: %d offers (%s) — hunting",
                             addr[:14], len(coll_offers), reason)

                    limited = coll_offers[:self.nonwl_qty]
                    report.total_offers_found += len(limited)

                    result = self._undercut_collection(addr, limited, chain, is_wl=False)
                    self._processed_collections.add(dedup_key)  # Mark as processed
                    report.undercuts_placed += result.our_offers_placed
                    report.nonwl_undercuts_placed += result.our_offers_placed
                    report.undercuts_failed += result.our_offers_failed
                    report.undercuts_skipped += result.our_offers_skipped

                    if self.collection_delay > 0 and not self.dry_run:
                        time.sleep(self.collection_delay)

            # ══════════════════════════════════════════════
            # PHASE 2b: SELL SIDE — be the cheapest listing, but above low_price
            # Runs BEFORE buy phases (3/3c) because those scan hundreds of
            # collections and can take 10+ minutes, blocking the sell phase.
            # ══════════════════════════════════════════════
            sell_enabled = self.buy_config.get("sell_settings", {}).get("enabled", False)
            if sell_enabled:
                log.info("═══ PHASE 2b: SELL SIDE on %s ═══", chain.upper())
                try:
                    sell_placed = self._run_sell_phase(chain)
                    log.info("PHASE 2b SELL %s: %d listings placed/updated", chain.upper(), sell_placed)
                except Exception as exc:
                    log.error("PHASE 2b SELL %s failed: %s", chain.upper(), exc, exc_info=True)

            # ══════════════════════════════════════════════
            # PHASE 3: MISSCLICK BUY — buy WL listings below max_buy_price
            # Scans cheapest listings on each WL collection.
            # If listing price ≤ max_buy_price from config → buy it.
            # ══════════════════════════════════════════════
            if self._buy_enabled and self._buyer:
                log.info("═══ PHASE 3: MISSCLICK BUY on %s ═══", chain.upper())
                try:
                    buys = self._run_missclick_buy_phase(chain, report)
                    log.info("PHASE 3 %s: %d buys executed", chain.upper(), buys)
                except Exception as exc:
                    log.error("PHASE 3 MISSCLICK BUY %s failed: %s", chain.upper(), exc, exc_info=True)

            # ══════════════════════════════════════════════
            # PHASE 3c: MISSCLICK BUY — non-WL collections from buy_config
            # Same logic as Phase 3, but only for collections NOT in WL.
            # Has its own dry_run flag (AUTO_BUY_CONFIG_DRY_RUN).
            # ══════════════════════════════════════════════
            if self._buy_enabled and self._buyer:
                log.info("═══ PHASE 3c: CONFIG BUY (non-WL) on %s ═══", chain.upper())
                try:
                    cfg_buys = self._run_missclick_buy_config_phase(chain, report)
                    log.info("PHASE 3c %s: %d buys executed", chain.upper(), cfg_buys)
                except Exception as exc:
                    log.error("PHASE 3c CONFIG BUY %s failed: %s", chain.upper(), exc, exc_info=True)

            # ══════════════════════════════════════════════
            # PHASE 4: PARASITE INTEL — scan parasite balances & approvals
            # Runs every 5th scan cycle to save API calls.
            # Logs which parasites have funds and marketplace approvals.
            # ══════════════════════════════════════════════
            if not hasattr(self, "_scan_cycle_count"):
                self._scan_cycle_count = 0
            self._scan_cycle_count += 1

            if self._scan_cycle_count % 5 == 1:  # every 5th cycle
                log.info("═══ PHASE 4: PARASITE INTEL on %s ═══", chain.upper())
                try:
                    intel = self._run_parasite_intel(chain)
                    log.info("PHASE 4 %s: %d parasites with balance, %d with offers",
                             chain.upper(), intel.get("with_balance", 0),
                             intel.get("with_offers", 0))
                except Exception as exc:
                    log.error("PHASE 4 %s failed: %s", chain.upper(), exc)

        report.collections_with_offers = len(report.by_collection)
        report.scan_duration_sec = time.time() - t0

        log.info(
            "🎯 Scan v4 complete in %.1fs: "
            "WL=%d offers/%d colls/%d undercuts, "
            "non-WL=%d offers/%d colls/%d undercuts, "
            "already_best=%d, wl_buys=%d/%d, cfg_buys=%d/%d",
            report.scan_duration_sec,
            report.wl_offers_found, report.wl_collections, report.wl_undercuts_placed,
            report.nonwl_offers_found, report.nonwl_collections, report.nonwl_undercuts_placed,
            report.wl_already_best, report.buy_success, report.buy_attempts,
            report.cfg_buy_success, report.cfg_buy_attempts
        )
        return report

    # ── Phase 3: Missclick Buy ──────────────────────────────────

    def _run_missclick_buy_phase(self, chain: str, report: ScanReport) -> int:
        """Phase 3: scan WL collections for cheap listings and buy them.

        Checks cheapest listing for each WL collection on this chain.
        If listing price <= max_buy_price from buy_config → execute buy.
        Respects per-collection and per-cycle buy limits.

        Returns number of successful buys.
        """
        if not self._buyer or not self._buy_enabled:
            return 0

        # Reset per-cycle counter at start of each chain scan
        self._cycle_buys = 0

        # Get WL collections for this chain
        wl_addrs = [
            addr for addr, info in self._wl_index.items()
            if info.get("network", info.get("chain", "")).lower() == chain
        ]

        if not wl_addrs:
            log.debug("Phase 3 %s: no WL collections for chain", chain.upper())
            return 0

        log.info("Phase 3 %s: scanning %d WL collections for cheap listings",
                 chain.upper(), len(wl_addrs))

        buys_done = 0
        now = time.time()

        for addr in wl_addrs:
            if self._stop_event.is_set():
                break

            # Per-cycle limit
            if self._cycle_buys >= self._max_buys_per_cycle:
                log.info("Phase 3: reached max buys per cycle (%d) — stopping",
                         self._max_buys_per_cycle)
                break

            # Per-collection cooldown
            cd_key = f"buy:{addr}:{chain}"
            last_buy = self._buy_cooldowns.get(cd_key, 0)
            if now - last_buy < self._buy_cooldown_seconds:
                continue

            # Get max buy price from config
            coll_cfg = self.buy_config.get("collections", {}).get(addr, {})
            if coll_cfg and not coll_cfg.get("enabled", True):
                continue

            max_buy = coll_cfg.get("max_buy_price", 0) if coll_cfg else 0
            buy_currency = (coll_cfg.get("currency") or "").upper() if coll_cfg else ""

            if not max_buy:
                # Fall back to chain defaults
                chain_defaults = self.buy_config.get("defaults", {}).get(chain, {})
                max_buy = chain_defaults.get("max_buy_price", 0)
                buy_currency = (chain_defaults.get("currency") or "").upper()

            if not max_buy or max_buy <= 0:
                continue

            # Map BNB→WBNB, ETH→WETH for consistency
            _WRAP = {"BNB": "WBNB", "ETH": "WETH"}
            buy_currency = _WRAP.get(buy_currency, buy_currency)

            name = (self._wl_index.get(addr, {}).get("collection_name")
                    or self._wl_index.get(addr, {}).get("name")
                    or addr[:14])

            report.buy_attempts += 1

            try:
                result = self._buyer.try_buy(
                    collection_address=addr,
                    collection_name=name,
                    chain=chain,
                    max_price=max_buy,
                    currency=buy_currency or ("WBNB" if chain == "bsc" else "WETH"),
                )

                if result is None:
                    # No listing found or buyer disabled
                    report.buy_skipped += 1
                    continue

                if result.dry_run:
                    report.buy_dry_run += 1
                    if result.error == "DRY_RUN":
                        log.info("  🏷️ Phase 3 DRY RUN: %s #%s @ %.6f %s (max=%.6f)",
                                 name, result.token_id, result.listing_price,
                                 result.currency, max_buy)
                elif result.success:
                    report.buy_success += 1
                    self._cycle_buys += 1
                    buys_done += 1
                    self._buy_cooldowns[cd_key] = time.time()
                    log.warning("  🔥 BOUGHT: %s #%s @ %.6f %s tx=%s",
                                name, result.token_id, result.listing_price,
                                result.currency, result.tx_hash or "?")
                    # Telegram alert
                    self._alert_buy(result, name, chain)
                else:
                    log.error("  ❌ BUY FAILED: %s — %s", name, result.error)

            except Exception as exc:
                log.error("Phase 3 buy error on %s: %s", name, exc)

            # Small delay between collections to avoid rate limits
            if self.collection_delay > 0:
                time.sleep(min(self.collection_delay, 1.0))

        return buys_done

    # ── Phase 3c: Missclick Buy (non-WL from buy_config) ──────

    # Currency → chain mapping for collections without explicit chain field
    _CURRENCY_TO_CHAIN: dict[str, str] = {
        "WBNB": "bsc", "BNB": "bsc", "BUSD": "bsc",
        "WETH": "eth", "ETH": "eth",
        "USDT": "bsc",   # default USDT to BSC (most OKX BSC NFTs use USDT)
        "USDC": "eth",   # default USDC to ETH
    }

    def _run_missclick_buy_config_phase(self, chain: str, report: ScanReport) -> int:
        """Phase 3c: scan non-WL collections from buy_config for cheap listings.

        Same logic as Phase 3 but:
        - Only scans collections in buy_config that are NOT in WL
        - Has its own dry_run flag (AUTO_BUY_CONFIG_DRY_RUN)
        - Uses currency to infer chain (no explicit chain field in config)

        Returns number of successful buys.
        """
        if not self._buyer or not self._buy_enabled:
            return 0

        all_colls = self.buy_config.get("collections", {})
        # Filter: non-WL, enabled, has max_buy_price, matches this chain
        addrs_to_scan = []
        for addr, cfg in all_colls.items():
            addr_lower = addr.lower()
            # Skip WL collections — already covered by Phase 3
            if addr_lower in self._wl_addrs_set:
                continue
            if not cfg.get("enabled", True):
                continue
            if not cfg.get("max_buy_price", 0) or cfg["max_buy_price"] <= 0:
                continue
            # Infer chain from currency
            cur = (cfg.get("currency") or "").upper()
            inferred_chain = self._CURRENCY_TO_CHAIN.get(cur, "")
            if inferred_chain != chain:
                continue
            addrs_to_scan.append((addr, cfg))

        if not addrs_to_scan:
            log.debug("Phase 3c %s: no non-WL config collections for chain", chain.upper())
            return 0

        log.info("Phase 3c %s: scanning %d non-WL config collections for cheap listings (dry_run=%s)",
                 chain.upper(), len(addrs_to_scan), self._cfg_buy_dry_run)

        buys_done = 0
        now = time.time()
        cycle_buys_3c = 0  # separate per-cycle counter for 3c

        for addr, cfg in addrs_to_scan:
            if self._stop_event.is_set():
                break

            # Per-cycle limit (shared with Phase 3)
            if cycle_buys_3c >= self._max_buys_per_cycle:
                log.info("Phase 3c: reached max buys per cycle (%d) — stopping",
                         self._max_buys_per_cycle)
                break

            # Per-collection cooldown
            cd_key = f"cfgbuy:{addr}:{chain}"
            last_buy = self._buy_cooldowns.get(cd_key, 0)
            if now - last_buy < self._buy_cooldown_seconds:
                continue

            max_buy = cfg["max_buy_price"]
            buy_currency = (cfg.get("currency") or "").upper()
            _WRAP = {"BNB": "WBNB", "ETH": "WETH"}
            buy_currency = _WRAP.get(buy_currency, buy_currency)
            name = cfg.get("name", addr[:14])

            report.cfg_buy_attempts += 1

            # Force dry_run for Phase 3c if AUTO_BUY_CONFIG_DRY_RUN=1
            # We temporarily override buyer's dry_run flag
            original_dry_run = self._buyer.dry_run
            if self._cfg_buy_dry_run:
                self._buyer.dry_run = True

            try:
                result = self._buyer.try_buy(
                    collection_address=addr,
                    collection_name=name,
                    chain=chain,
                    max_price=max_buy,
                    currency=buy_currency or ("WBNB" if chain == "bsc" else "WETH"),
                )

                if result is None:
                    report.cfg_buy_skipped += 1
                    continue

                if result.dry_run:
                    report.cfg_buy_dry_run += 1
                    if result.error == "DRY_RUN":
                        log.info("  🏷️ Phase 3c DRY RUN: %s #%s @ %.6f %s (max=%.6f)",
                                 name, result.token_id, result.listing_price,
                                 result.currency, max_buy)
                elif result.success:
                    report.cfg_buy_success += 1
                    cycle_buys_3c += 1
                    buys_done += 1
                    self._buy_cooldowns[cd_key] = time.time()
                    log.warning("  🔥 CFG BOUGHT: %s #%s @ %.6f %s tx=%s",
                                name, result.token_id, result.listing_price,
                                result.currency, result.tx_hash or "?")
                    self._alert_buy(result, name, chain)
                else:
                    log.error("  ❌ CFG BUY FAILED: %s — %s", name, result.error)

            except Exception as exc:
                log.error("Phase 3c buy error on %s: %s", name, exc)
            finally:
                # Restore original dry_run
                self._buyer.dry_run = original_dry_run

            # Rate limit delay
            if self.collection_delay > 0:
                time.sleep(min(self.collection_delay, 1.0))

        return buys_done

    def _alert_buy(self, result, name: str, chain: str):
        """Send Telegram alert for a successful buy."""
        if not self.tg_bot_token or not self.tg_chat_id:
            return
        try:
            import requests as _req
            msg = (
                f"🔥 <b>MISSCLICK BUY</b>\n"
                f"Collection: {name}\n"
                f"Token: #{result.token_id}\n"
                f"Price: {result.listing_price:.6f} {result.currency}\n"
                f"Chain: {chain.upper()}\n"
                f"TX: <code>{result.tx_hash or '?'}</code>"
            )
            _req.post(
                f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage",
                json={"chat_id": self.tg_chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as exc:
            log.debug("Telegram buy alert failed: %s", exc)

    def _fetch_wallet_offers(self, chain: str) -> list[ParasiteOffer]:
        """Fetch active offers from ALL parasite wallets on a given chain.

        Iterates over all target_wallets. Fetches BOTH token + collection offers.
        """
        all_offers: list[ParasiteOffer] = []

        for i, wallet in enumerate(sorted(self.target_wallets)):
            if self._stop_event.is_set():
                break

            wallet_offers = self._fetch_single_wallet(wallet, chain)
            if wallet_offers:
                log.info("🎯 %s wallet %d/%d (%s...%s): %d offers",
                         chain.upper(), i + 1, len(self.target_wallets),
                         wallet[:8], wallet[-4:], len(wallet_offers))
                all_offers.extend(wallet_offers)

            # Small delay between wallets to avoid rate limiting
            if i < len(self.target_wallets) - 1:
                time.sleep(0.3)

        return all_offers

    def _fetch_single_wallet(self, wallet: str, chain: str) -> list[ParasiteOffer]:
        """Fetch offers from a single wallet on a chain."""
        offers: list[ParasiteOffer] = []

        # Strategy 1: Use authenticated OKX API
        client = self._get_okx_client()
        if client:
            try:
                raw_offers = self._fetch_raw_wallet(client, chain, wallet)
                for raw in raw_offers:
                    offers.append(self._raw_to_parasite(raw, chain))
                if offers:
                    return offers
            except Exception as exc:
                log.warning("API scan failed for %s...%s on %s: %s", wallet[:8], wallet[-4:], chain.upper(), exc)

        # Strategy 2: Direct HTTP fallback (priapi)
        return self._fetch_wallet_offers_priapi(chain, wallet)

    def _fetch_raw_wallet(self, client, chain: str, wallet: str = "") -> list[dict]:
        """Fetch raw offer dicts from OKX API for a specific wallet.

        Tries both regular offers and collection offers endpoints.
        """
        target = wallet or list(self.target_wallets)[0]
        mc = client._market_client()
        all_raw: list[dict] = []

        # 1) Regular per-item offers via get_offers()
        # BSC: /offers with maker filter always returns 0 — skip entirely
        if chain != "bsc":
            cursor = None
            for _ in range(20):
                payload = mc.get_offers(
                    chain=chain,
                    maker=target,
                    status="active",
                    limit=100,
                    cursor=cursor,
                )
                items = self._extract_items(payload)
                if items:
                    log.info("🎯 %s API: %d raw items (token offers)", chain.upper(), len(items))
                    if log.isEnabledFor(logging.DEBUG) and items:
                        import json as _json
                        log.debug("🔍 Sample raw token offer: %s",
                                  _json.dumps(items[0], ensure_ascii=False, default=str)[:500])
                    all_raw.extend(items)
                cursor = self._extract_cursor(payload)
                if not cursor:
                    break

        # 2) Collection offers — dedicated /collection-offers endpoint
        # NOTE: This is the ONLY way to find BSC collection offers by maker.
        #       The regular /offers endpoint ignores maker filter on BSC.
        #       Do NOT pass offerType param — it breaks the response.
        try:
            cursor = None
            for _ in range(20):
                payload = client._request(
                    method="GET",
                    path="/api/v5/mktplace/nft/markets/collection-offers",
                    params={
                        "chain": chain,
                        "maker": target,
                        "status": "active",
                        "limit": 100,
                        "cursor": cursor,
                    },
                )
                items = self._extract_items(payload)
                if items:
                    log.info("🎯 %s API: %d raw items (collection-offers by maker)",
                             chain.upper(), len(items))
                    if log.isEnabledFor(logging.DEBUG) and items:
                        import json as _json
                        log.debug("🔍 Sample raw collection offer: %s",
                                  _json.dumps(items[0], ensure_ascii=False, default=str)[:500])
                    all_raw.extend(items)
                cursor = self._extract_cursor(payload)
                if not cursor:
                    break
        except Exception as exc:
            log.warning("Collection-offers endpoint failed for %s...%s: %s",
                        target[:8], target[-4:], exc)

        log.info("🎯 %s total raw offers from API: %d", chain.upper(), len(all_raw))
        return all_raw

    @staticmethod
    def _extract_items(payload: dict) -> list[dict]:
        """Extract offer records from API response."""
        data = payload.get("data")
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            inner = data.get("data") or data.get("orders") or data.get("records") or data.get("offers")
            if isinstance(inner, list):
                return [i for i in inner if isinstance(i, dict)]
        return []

    @staticmethod
    def _extract_cursor(payload: dict) -> str | None:
        data = payload.get("data")
        if isinstance(data, dict):
            c = data.get("cursor") or data.get("nextCursor")
            return None if c in {None, "", "0"} else str(c)
        return None

    def _fetch_top_offers_for_collection(self, collection_address: str,
                                         chain: str,
                                         limit: int = 5) -> list[ParasiteOffer]:
        """Fetch top (highest price) offers for a WL collection from anyone.

        Used in Phase 1: check who has the best offers and undercut non-friends.
        Uses authenticated web3.okx.com API (same as Phase 2) — NOT priapi.
        Queries BOTH token-level and collection-level offer endpoints.
        Returns list sorted by price desc (highest first).
        """
        client = self._get_okx_client()
        if not client:
            log.warning("WL Phase1: no OKX client available for %s", collection_address[:14])
            return []

        all_offers: list[ParasiteOffer] = []

        # BSC: /offers endpoint always returns 0 with collectionAddress filter,
        # only /collection-offers works. ETH: both work, query both.
        if chain == "bsc":
            api_paths = ["/api/v5/mktplace/nft/markets/collection-offers"]
        else:
            api_paths = [
                "/api/v5/mktplace/nft/markets/offers",
                "/api/v5/mktplace/nft/markets/collection-offers",
            ]
        for path in api_paths:
            try:
                payload = client._request(
                    method="GET",
                    path=path,
                    params={
                        "chain": chain,
                        "collectionAddress": collection_address,
                        "status": "active",
                        "limit": limit,
                    },
                )
                items = self._extract_items(payload)
                if items:
                    all_offers.extend(self._raw_to_parasite(r, chain) for r in items)
            except Exception as exc:
                log.warning("WL Phase1 fetch failed for %s (%s): %s",
                            collection_address[:14], path.split("/")[-1], exc)

        if not all_offers:
            return []

        # Deduplicate and sort by price desc
        seen: set[str] = set()
        unique: list[ParasiteOffer] = []
        for o in all_offers:
            key = f"{o.maker}:{o.collection_address}:{o.price}:{o.currency}"
            if key not in seen:
                seen.add(key)
                unique.append(o)
        unique.sort(key=lambda o: o.price, reverse=True)
        return unique[:limit]

    def _fetch_wallet_offers_priapi(self, chain: str, wallet: str = "") -> list[ParasiteOffer]:
        """Fallback: fetch wallet offers from priapi.

        NOTE: The old priapi/v5/nft/ec/ endpoints are DEAD (404 since ~2026-03).
        The new priapi/v1/nft/order/offers requires nftId, not maker — so
        per-wallet scanning is not possible via priapi anymore.
        Authenticated API (_fetch_raw_wallet) is now the ONLY source for
        wallet-level offer scanning.
        """
        log.debug("priapi wallet-scan unavailable (endpoints deprecated) — "
                   "relying on authenticated API for wallet %s...%s",
                   (wallet or "?")[:8], (wallet or "?")[-4:])
        return []

    # ── Single collection reactive hunt ───────────────────────

    def hunt_collection(self, collection_address: str, collection_name: str,
                        chain: str) -> HuntResult | None:
        """Reactive hunt — triggered by sales_stream on parasite trade detection."""
        if not self.enabled:
            return None

        addr = collection_address.lower()
        now = time.time()
        with self._lock:
            last = self._cooldowns.get(addr, 0)
            if now - last < 60:  # 1 min cooldown per collection for reactive
                return None
            self._cooldowns[addr] = now

        offers = self._fetch_collection_offers(addr, chain)
        if not offers:
            return HuntResult(addr, collection_name, chain, 0, 0, 0, 0)

        is_wl = addr in self._wl_index
        log.info("🎯 Reactive: %s — %d parasite offers (WL=%s)", collection_name, len(offers), is_wl)
        return self._undercut_collection(addr, offers, chain, is_wl=is_wl)

    def _fetch_collection_offers(self, collection_address: str,
                                  chain: str) -> list[ParasiteOffer]:
        """Fetch parasite offers for a specific collection (all target wallets)."""
        all_offers: list[ParasiteOffer] = []

        for wallet in sorted(self.target_wallets):
            # Provider path
            provider = self._get_provider()
            if provider:
                try:
                    raw = provider.fetch_all_pages(
                        chain=chain,
                        collection_address=collection_address,
                        maker=wallet,
                        status="active",
                        max_pages=3,
                    )
                    all_offers.extend(
                        self._normalized_to_parasite(o, chain) for o in raw
                    )
                    continue
                except Exception:
                    pass

            # Fallback: priapi/v1/nft/order/offers via nftId
            addr = collection_address.lower()
            page_data = self._resolve_collection_page_data(addr, chain)
            nft_id = page_data.get("nft_id", "")
            if nft_id:
                try:
                    raw_offers = self._fetch_offers_by_nftid(nft_id, max_pages=2)
                    for item in raw_offers:
                        parsed = self._parse_v1_offer(item, chain)
                        if parsed["maker"] == wallet:
                            all_offers.append(ParasiteOffer(
                                collection_address=addr,
                                collection_name=addr[:14],
                                chain=chain,
                                maker=parsed["maker"],
                                price=parsed["price"],
                                currency=parsed["currency"],
                                token_id="",
                                order_id=parsed["order_id"] or parsed["order_hash"],
                            ))
                except Exception as exc:
                    log.debug("priapi v1 collection offers failed for %s: %s",
                              addr[:14], exc)

        return all_offers

    # ── Undercut logic ────────────────────────────────────────

    def _undercut_collection(self, collection_address: str,
                              offers: list[ParasiteOffer],
                              chain: str,
                              is_wl: bool = True) -> HuntResult:
        """Outbid parasite offers for a single collection.

        Logic:
        1. Parasite offers at price P in currency C
        2. Our outbid = P * (1 + undercut_bps/10000) in SAME currency C
        3. If we don't hold currency C, convert to our preferred currency
        4. Cap: collection max_offer_price > WL max_usd / non-WL nonwl_max_usd
        5. If outbid > cap, we offer at cap
        """
        addr = collection_address.lower()
        name = offers[0].collection_name if offers else addr[:14]

        placed = 0
        failed = 0
        skipped = 0

        # ── Find the REAL best enemy offer (highest USD) ──
        # First check passed parasite offers, then also fetch the actual
        # top offer on the collection.  We must beat whoever is #1,
        # not just the tracked parasites.
        best_enemy: ParasiteOffer | None = None
        best_enemy_usd: float = 0.0
        for offer in offers:
            if offer.price <= 0:
                continue
            # Skip offers with empty maker — could be our own offer without maker info
            if not offer.maker:
                log.debug("  ⚠ %s: skipping offer with empty maker (%.4f %s) — anti-self-bid",
                          name, offer.price, offer.currency)
                skipped += 1
                continue
            if offer.maker in self.protected_wallets:
                self._friend_skipped_count += 1
                skipped += 1
                continue
            offer_usd = self.prices.to_usd(offer.price, offer.currency.upper())
            if offer_usd <= 0:
                continue
            if best_enemy is None or offer_usd > best_enemy_usd:
                best_enemy = offer
                best_enemy_usd = offer_usd

        # Also check the REAL top ENEMY offer on the collection (may be from
        # a non-tracked wallet).  _fetch_best_offer_maker now returns
        # the best ENEMY (excluding our/friend wallets) directly.
        real_top = self._fetch_best_offer_maker(addr, chain)
        rt_enemy = real_top.get("_best_enemy", real_top)
        rt_usd = rt_enemy.get("usd", 0) if rt_enemy.get("maker") else 0
        if rt_usd > best_enemy_usd:
            rt_price = rt_enemy["price"]
            rt_cur = rt_enemy["currency"]
            rt_maker = rt_enemy["maker"]
            log.info("  🔍 %s: real top enemy is %.4f %s ($%.2f) from %s — higher than tracked parasites ($%.2f)",
                     name, rt_price, rt_cur, rt_usd, rt_maker[:14], best_enemy_usd)
            best_enemy = ParasiteOffer(
                collection_address=addr,
                collection_name=name,
                chain=chain,
                token_id="",
                price=rt_price,
                currency=rt_cur,
                offer_id="",
                source_type="real_top",
                maker=rt_maker,
                expires_at="",
            )
            best_enemy_usd = rt_usd

        # Choose our offer currency
        if best_enemy:
            log.info("  🎯 %s: FINAL best_enemy=%.6f %s ($%.4f) maker=%s src=%s",
                     name, best_enemy.price, best_enemy.currency, best_enemy_usd,
                     best_enemy.maker[:14] if best_enemy.maker else "?",
                     getattr(best_enemy, "source_type", "tracked"))
        else:
            log.info("  🎯 %s: no enemy found — min-offer mode", name)
        chain_native = {"eth": "WETH", "bsc": "WBNB"}.get(chain, "WETH")
        chain_currencies = {
            "eth": {"WETH", "USDT", "USDC", "DAI"},
            "bsc": {"WBNB", "USDT", "USDC", "BUSD"},
        }.get(chain, {"WETH"})

        # Read collection config for currency preference
        coll_cfg = self.buy_config.get("collections", {}).get(addr, {})
        cfg_cur = (coll_cfg.get("currency") or "").upper()

        # Map native coin names to wrapped equivalents (OKX API returns
        # "BNB"/"ETH" but Seaport uses WBNB/WETH for offers).
        # BUSD→USDT: OKX treats BUSD≡USDT on BSC, parasites often bid in
        # BUSD but we outbid in USDT (same $1 value).
        _WRAP_MAP = {"BNB": "WBNB", "ETH": "WETH", "BUSD": "USDT"}

        if best_enemy is not None:
            parasite_cur = best_enemy.currency.upper()
            # Normalise: BNB→WBNB, ETH→WETH, BUSD→USDT
            parasite_cur_mapped = _WRAP_MAP.get(parasite_cur, parasite_cur)
            if parasite_cur != parasite_cur_mapped:
                log.info("  💱 %s: enemy currency %s → mapped to %s for our offer",
                         name, parasite_cur, parasite_cur_mapped)
            if parasite_cur_mapped in chain_currencies and parasite_cur_mapped in self.offer_currencies:
                our_cur = parasite_cur_mapped
            elif cfg_cur and cfg_cur in chain_currencies:
                our_cur = cfg_cur
            elif chain_native in self.offer_currencies:
                our_cur = chain_native
            elif chain_currencies & set(self.offer_currencies):
                our_cur = next(iter(chain_currencies & set(self.offer_currencies)))
            else:
                our_cur = chain_native
        else:
            # No enemy — use config currency or chain native
            our_cur = cfg_cur if cfg_cur and cfg_cur in chain_currencies else chain_native

        # Determine cap
        global_cap = self.max_usd if is_wl else self.nonwl_max_usd
        coll_max = self._get_max_price(addr, chain, our_cur)
        has_collection_config = coll_max is not None
        if has_collection_config:
            coll_max_usd = self.prices.to_usd(coll_max, our_cur)
            cap_usd = coll_max_usd if coll_max_usd > 0 else global_cap
        else:
            cap_usd = global_cap

        # ── TWO-TIER SYSTEM ──
        # WL collections: qty=40 (много офферов, ловим мисклики)
        # Non-WL collections: qty=1 (минимум, просто перебиваем паразита)
        is_premium = has_collection_config and cap_usd > self.max_usd
        default_qty = 40 if is_wl else 1
        should_cancel_old = True  # ALWAYS cancel old offers to prevent stacking
        offer_duration_hours = 720  # 30 days

        if best_enemy is None:
            # No enemy → check if we already have an offer (avoid duplicates!)
            our_existing = self._find_our_offer(addr, chain)
            if our_existing:
                log.info("  ✅ %s: no enemy, we already have offer (%.4f %s) — skip",
                         name, our_existing["price"], our_existing["currency"])
                self.already_winning += 1
                return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

            # No enemy, no existing offer → place at minimum price
            min_prices = {"WETH": 0.0001, "WBNB": 0.0001, "USDT": 0.01, "USDC": 0.01, "BUSD": 0.01, "DAI": 0.01}
            our_price = min_prices.get(our_cur, 0.0001)
            our_usd = self.prices.to_usd(our_price, our_cur)
            # Still cap it
            if our_usd > cap_usd:
                our_usd = cap_usd
                our_price = self.prices.from_usd(our_usd, our_cur)
            quantity = coll_cfg.get("max_offers", default_qty)
            log.info("  📎 %s: NO enemy → min offer %.4f %s ($%.2f) qty=%d tier=%s%s",
                     name, our_price, our_cur, our_usd, quantity,
                     "premium" if is_premium else "basic",
                     " [DRY]" if self.dry_run else "")
        else:
            # Enemy found — outbid by MINIMUM STEP (never jump to max price!)
            offer = best_enemy
            parasite_cur = offer.currency.upper()
            parasite_usd = self.prices.to_usd(offer.price, parasite_cur)
            if parasite_usd <= 0:
                log.info("  ⏭ SKIP %s #%s: can't get USD price for %s",
                         name, offer.token_id or "col", parasite_cur)
                return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

            # Calculate outbid price: enemy price + small step (undercut_bps)
            outbid_usd = parasite_usd * (1 + self.undercut_bps / 10000)
            if outbid_usd > cap_usd:
                outbid_usd = cap_usd

            our_price = self.prices.from_usd(outbid_usd, our_cur)
            our_usd = outbid_usd

        if our_price < 0.000001:
            return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

        # ── BEST OFFER RELEVANCE CHECK ──
        # If our capped offer is < 20% of the best enemy offer, skip.
        # Placing a $0.54 offer on a collection where best offer is $242
        # is pointless — no one will sell that far below market.
        if best_enemy is not None:
            best_enemy_usd = self.prices.to_usd(best_enemy.price, best_enemy.currency.upper())
            if best_enemy_usd > 0 and our_usd < best_enemy_usd * 0.20:
                log.info("  🚫 %s: our $%.2f < 20%% of best offer $%.2f — SKIP (too far below market)",
                         name, our_usd, best_enemy_usd)
                return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

        # ── FLOOR PRICE GUARD ──
        # NEVER place an offer >= floor price.  If our offer is at or above
        # floor, someone can buy the NFT at floor and instantly accept our
        # offer for a risk-free profit at our expense.
        floor_usd = self._fetch_floor_price(addr, chain)
        if floor_usd > 0:
            if our_usd >= floor_usd:
                # Cap our offer to 95% of floor so there's no arbitrage
                safe_usd = floor_usd * 0.95
                if safe_usd <= 0:
                    log.warning("  🚫 %s: offer $%.2f ≥ floor $%.2f — SKIP (floor too low)",
                                name, our_usd, floor_usd)
                    return HuntResult(addr, name, chain, len(offers), 0, 0, 1)
                log.warning("  🚫 %s: offer $%.2f ≥ floor $%.2f — capping to $%.2f (95%% of floor)",
                            name, our_usd, floor_usd, safe_usd)
                our_usd = safe_usd
                our_price = self.prices.from_usd(our_usd, our_cur)
        elif is_premium:
            # Premium collection but floor unknown — too risky, skip
            log.warning("  🚫 %s: premium offer $%.2f but floor UNKNOWN — SKIP for safety",
                        name, our_usd)
            return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

        # ── No-enemy path: place offer with qty ──
        if best_enemy is None:
            # quantity already set above
            if self.dry_run:
                placed += 1
            else:
                # Anti-stacking cooldown check
                cooldown_key = f"{addr}:{chain}"
                last_ts = self._last_placed.get(cooldown_key, 0)
                if time.time() - last_ts < self._PLACE_COOLDOWN:
                    mins_ago = (time.time() - last_ts) / 60
                    log.info("  ⏳ %s: cooldown active (placed %.1f min ago) — SKIP to prevent stacking", name, mins_ago)
                    return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

                # Cancel old offers first to prevent stacking
                if should_cancel_old:
                    cancel_ok = self._cancel_existing_offer(addr, chain, name)
                    if not cancel_ok:
                        log.warning("  🚫 %s: cancel failed — ABORT new offer to prevent stacking", name)
                        return HuntResult(addr, name, chain, len(offers), 0, 1, 0)

                try:
                    ok = self._submit_undercut(
                        addr, "", our_price, our_cur, chain,
                        quantity=quantity,
                        duration_hours=offer_duration_hours,
                    )
                    # ALWAYS set cooldown — even on failure (might have succeeded on-chain)
                    self._last_placed[cooldown_key] = time.time()
                    if ok:
                        placed += 1
                    else:
                        failed += 1
                except Exception as exc:
                    log.error("  Min-offer failed %s: %s", name, exc)
                    self._last_placed[cooldown_key] = time.time()
                    failed += 1

                if self.delay > 0:
                    time.sleep(self.delay)

            return HuntResult(addr, name, chain, len(offers), placed, failed, skipped)

        # ── Enemy path: outbid the top parasite ──
        offer = best_enemy
        parasite_cur = offer.currency.upper()
        parasite_usd = self.prices.to_usd(offer.price, parasite_cur)

        # Check if we already hold the best (highest USD) offer — skip if so
        if self._we_already_best(addr, offer.token_id or "", chain):
            self.already_winning += 1
            return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

        # ── SMART OFFER CHECK: don't waste gas on pointless cancel+replace ──
        # 1. If our offer already >= enemy → check if we're WAY too high
        #    and should LOWER to enemy + min step (save money).
        # 2. If difference is small → KEEP (not worth cancel+replace).
        # 3. If our offer < enemy → proceed to cancel+replace (outbid).
        min_step_mult = 1 + self.undercut_bps / 10000  # e.g. 1.005 for 50 bps
        our_existing = self._find_our_offer(addr, chain)
        if our_existing:
            existing_usd = self.prices.to_usd(
                our_existing["price"], our_existing["currency"].upper()
            )
            target_usd = parasite_usd * min_step_mult  # enemy + minimum step
            # Cap target to collection max
            if target_usd > cap_usd:
                target_usd = cap_usd

            if existing_usd > 0 and existing_usd >= parasite_usd:
                # We're already above enemy.
                # Only LOWER if we're paying >20% more than needed.
                # e.g. enemy=$0.10, target=$0.1005, threshold=$0.1206
                # If our $0.1045 < $0.1206 → KEEP (not worth the churn)
                lower_threshold = target_usd * 1.20  # 20% above target
                if existing_usd <= lower_threshold:
                    # Reasonable range — KEEP as-is
                    log.info("  ✅ %s: our $%.4f ≥ enemy $%.4f, within 20%% of target $%.4f — KEEP",
                             name, existing_usd, parasite_usd, target_usd)
                    self.already_winning += 1
                    return HuntResult(addr, name, chain, len(offers), 0, 0, 1)
                else:
                    # Way too high above enemy — LOWER to enemy + min step
                    log.info("  📉 %s: our $%.4f > 20%% above target $%.4f (enemy $%.4f) — LOWERING",
                             name, existing_usd, target_usd, parasite_usd)
                    our_usd = target_usd
                    our_price = self.prices.from_usd(our_usd, our_cur)

        # Read quantity from collection config, or use tier default
        quantity = coll_cfg.get("max_offers", default_qty)

        # ── Check if outbid price exceeds cap ──
        # Check BEFORE cancelling — if enemy is above our cap, we may want
        # to keep our existing offer instead of cancelling+replacing.
        enemy_above_cap = (parasite_usd >= cap_usd) and (coll_max is not None)

        if enemy_above_cap:
            our_existing = self._find_our_offer(addr, chain)
            if our_existing:
                log.info("  ⏸ %s: enemy=%.2f$ ≥ cap=%.2f$ — keeping our offer (%.4f %s)",
                         name, parasite_usd, cap_usd,
                         our_existing["price"], our_existing["currency"])
                return HuntResult(addr, name, chain, len(offers), 0, 0, 1)
            else:
                log.info("  📎 %s: enemy=%.2f$ ≥ cap=%.2f$ — placing at cap %.4f %s",
                         name, parasite_usd, cap_usd, our_price, our_cur)

        # Anti-stacking cooldown check (enemy path)
        cooldown_key = f"{addr}:{chain}"
        last_ts = self._last_placed.get(cooldown_key, 0)
        if time.time() - last_ts < self._PLACE_COOLDOWN:
            mins_ago = (time.time() - last_ts) / 60
            log.info("  ⏳ %s: cooldown active (placed %.1f min ago) — SKIP to prevent stacking", name, mins_ago)
            return HuntResult(addr, name, chain, len(offers), 0, 0, 1)

        # We are NOT #1 → cancel ALL old offers, then place new one above enemy
        cancel_ok = self._cancel_existing_offer(addr, chain, name)
        if not cancel_ok:
            log.warning("  🚫 %s: cancel failed — ABORT new offer to prevent stacking", name)
            return HuntResult(addr, name, chain, len(offers), 0, 1, 0)

        display_tid = offer.token_id if offer.token_id and offer.token_id != "0" else "col"
        log.info(
            "  📎 %s #%s: паразит=%.4f %s ($%.2f) → наш=%.4f %s ($%.2f) qty=%d tier=%s%s",
            name, display_tid,
            offer.price, parasite_cur, parasite_usd,
            our_price, our_cur, our_usd, quantity,
            "premium" if is_premium else "basic",
            " [DRY]" if self.dry_run else ""
        )

        if self.dry_run:
            placed += 1
            self._alert_undercut(
                name, offer.token_id or "collection",
                chain, offer.price, our_price,
                parasite_cur, our_cur,
                parasite_usd, our_usd, dry=True,
                collection_address=addr,
            )
        else:
            try:
                ok = self._submit_undercut(
                    addr, offer.token_id or "", our_price, our_cur, chain,
                    quantity=quantity,
                    duration_hours=offer_duration_hours,
                )
                # ALWAYS set cooldown — even on failure (might have succeeded on-chain)
                self._last_placed[cooldown_key] = time.time()
                if ok:
                    placed += 1
                    self._alert_undercut(
                        name, offer.token_id or "collection",
                        chain, offer.price, our_price,
                        parasite_cur, our_cur,
                        parasite_usd, our_usd, dry=False,
                        collection_address=addr,
                    )
                    # Mark cache so we don't bid again this scan
                    cache_key_done = f"{addr}:{chain}"
                    self._best_offer_cache[cache_key_done] = {
                        "maker": self.our_wallet,
                        "price": our_price,
                        "currency": our_cur,
                        "usd": our_usd,
                    }
                else:
                    failed += 1
            except Exception as exc:
                log.error("  Undercut failed #%s: %s", offer.token_id, exc)
                self._last_placed[cooldown_key] = time.time()
                failed += 1

            if self.delay > 0:
                time.sleep(self.delay)

        return HuntResult(addr, name, chain, len(offers), placed, failed, skipped)

    # ── Best-offer check ────────────────────────────────────────

    def _we_already_best(self, collection_address: str, token_id: str,
                         chain: str) -> bool:
        """Check if our wallet already holds the best (highest USD) offer.

        Uses per-scan cache: one HTTP call per collection (not per offer).
        Returns True if we should SKIP placing a new offer.
        On API error, returns False (proceed with undercut to be safe).
        """
        if not self.our_wallet:
            return False

        cache_key = f"{collection_address}:{chain}"

        if cache_key not in self._best_offer_cache:
            self._best_offer_cache[cache_key] = self._fetch_best_offer_maker(
                collection_address, chain
            )

        cached = self._best_offer_cache[cache_key]
        # Use _overall_best (includes our own offers) — if the absolute
        # best offer is from our/friend wallet, we're already #1.
        overall = cached.get("_overall_best", cached)
        best_maker = overall.get("maker", "")
        if best_maker and best_maker in self.protected_wallets:
            log.info("  ✅ ALREADY BEST %s — skip (our/friend: %.4f %s $%.2f)",
                     collection_address[:14],
                     overall.get("price", 0), overall.get("currency", "?"),
                     overall.get("usd", 0))
            return True
        return False

    # ── Collection page data resolution (new OKX API) ─────────

    def _get_collection_slug(self, collection_address: str) -> str | None:
        """Resolve contract address → OKX page slug.

        Sources (in priority order):
        1. OKX_SLUG_MAP env var
        2. _wl_index (whitelist) — collection_name often IS the slug
        """
        addr = collection_address.lower()
        if addr in self._slug_map:
            return self._slug_map[addr]
        # Try whitelist: collection_name is sometimes the OKX slug
        wl_info = self._wl_index.get(addr, {})
        cname = wl_info.get("collection_name") or wl_info.get("name") or ""
        if cname:
            # Convert name to slug-like format (lowercase, hyphenated)
            slug_candidate = cname.lower().strip().replace(" ", "-")
            slug_candidate = _re.sub(r"[^a-z0-9-]", "", slug_candidate)
            if slug_candidate:
                return slug_candidate
        return None

    def _resolve_collection_page_data(self, collection_address: str,
                                       chain: str) -> dict:
        """Fetch OKX collection page HTML and parse SSR data.

        Returns {"nft_id": str, "best_offer": dict, "slug": str} or empty dict.
        best_offer = {"price": float, "currency": str, "usd": float}

        The page HTML contains <script>{"appContext": ...}</script> with
        collectionInfo.nftData.list[N].id (= nftId) and .bestOffer.
        """
        addr = collection_address.lower()
        cache_key = f"{addr}:{chain}"

        # Check cache
        cached = self._collection_page_cache.get(cache_key)
        if cached and (time.time() - cached.get("ts", 0)) < self._COLLECTION_PAGE_TTL:
            return cached

        chain_name = {"eth": "eth", "bsc": "bsc", "polygon": "polygon",
                      "arbitrum": "arbitrum"}.get(chain, "bsc")
        slug = self._get_collection_slug(addr)
        if not slug:
            log.debug("_resolve_collection_page_data: no slug for %s", addr[:14])
            return {}

        try:
            from curl_cffi import requests as http
        except ImportError:
            import requests as http  # type: ignore

        url = f"https://web3.okx.com/nft/collection/{chain_name}/{slug}"
        try:
            resp = http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
            }, timeout=15, impersonate="chrome")

            if resp.status_code != 200:
                log.warning("Collection page %s returned %d", slug, resp.status_code)
                return {}

            # Parse SSR JSON from <script>{"appContext":...}</script>
            scripts = _re.findall(
                r'<script[^>]*>(\{"appContext".*?)</script>',
                resp.text, _re.DOTALL,
            )
            if not scripts:
                log.warning("Collection page %s: no appContext script found", slug)
                return {}

            data = _json.loads(scripts[0])
            coll_info = (data.get("appContext", {})
                         .get("initialProps", {})
                         .get("collectionInfo", {}))
            nft_data = coll_info.get("nftData", {})
            items = nft_data.get("list", [])

            if not items:
                log.warning("Collection page %s: nftData.list is empty", slug)
                return {}

            # Get nftId from first item
            nft_id = str(items[0].get("id", ""))
            if not nft_id or nft_id == "0":
                log.warning("Collection page %s: first item has no valid id", slug)
                return {}

            # Get bestOffer from first item (same for all items in collection)
            best_raw = items[0].get("bestOffer") or {}
            default_currency = "ETH" if chain == "eth" else "BNB"
            best_price = float(best_raw.get("price", 0) or 0)
            best_currency = self._resolve_currency(
                best_raw.get("currency", ""), chain, default_currency)
            best_usd = float(best_raw.get("usdPrice", 0) or 0)
            if best_usd == 0 and best_price > 0:
                best_usd = self.prices.to_usd(best_price, best_currency)

            result = {
                "nft_id": nft_id,
                "slug": slug,
                "best_offer": {
                    "price": best_price,
                    "currency": best_currency,
                    "usd": best_usd,
                },
                "ts": time.time(),
            }

            self._collection_page_cache[cache_key] = result
            log.info("  📄 Collection page %s: nftId=%s bestOffer=%.4f %s ($%.4f)",
                     slug, nft_id, best_price, best_currency, best_usd)
            return result

        except Exception as exc:
            log.warning("_resolve_collection_page_data %s failed: %s", slug, exc)
            return {}

    def _fetch_offers_by_nftid(self, nft_id: str,
                                max_pages: int = 3) -> list[dict]:
        """Fetch ALL offers for a collection using the working priapi/v1 endpoint.

        Uses per-scan cache (_nftid_offers_cache) to avoid N+1 queries when
        multiple methods query the same nftId in one scan cycle.

        Uses nftId (OKX internal ID) — the ONLY endpoint that still works
        after OKX deprecated priapi/v5/nft/ec/* endpoints.

        Returns list of raw offer dicts with normalized fields:
        {"maker": str, "price": float, "currency": str, "order_id": str,
         "order_hash": str, "order_type": int, "raw": dict}
        """
        # Per-scan cache hit — avoid redundant HTTP calls
        if nft_id in self._nftid_offers_cache:
            return self._nftid_offers_cache[nft_id]

        try:
            from curl_cffi import requests as http
        except ImportError:
            import requests as http  # type: ignore

        max_pages = min(max_pages, 20)  # safety cap
        all_items: list[dict] = []
        for page in range(1, max_pages + 1):
            try:
                resp = http.get(
                    "https://web3.okx.com/priapi/v1/nft/order/offers",
                    params={
                        "showAllOrder": "true",
                        "showCollOrder": "true",
                        "page": str(page),
                        "pageSize": "50",
                        "nftId": nft_id,
                        "t": str(int(time.time() * 1000)),
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    },
                    timeout=12,
                    impersonate="chrome",
                )
                data = resp.json()
                if data.get("code") not in (0, "0"):
                    log.warning("offers_by_nftid page %d: code=%s msg=%s",
                                page, data.get("code"), str(data.get("msg", ""))[:60])
                    break

                raw_data = data.get("data", {})
                items = []
                if isinstance(raw_data, list):
                    items = raw_data
                elif isinstance(raw_data, dict):
                    for k in ("offers", "data", "list", "items"):
                        v = raw_data.get(k)
                        if isinstance(v, list) and v:
                            items = v
                            break
                    if not items and isinstance(raw_data, dict):
                        # Sometimes data IS the list wrapper
                        items = [raw_data] if "ownerAddress" in raw_data else []

                if not items:
                    break

                all_items.extend(items)

                # Stop if we got fewer than pageSize (last page)
                if len(items) < 50:
                    break

            except Exception as exc:
                log.warning("offers_by_nftid page %d failed: %s", page, exc)
                break

            time.sleep(0.2)  # rate limit

        # Store in per-scan cache
        self._nftid_offers_cache[nft_id] = all_items
        return all_items

    def _parse_v1_offer(self, item: dict, chain: str) -> dict:
        """Parse a single offer from priapi/v1/nft/order/offers response.

        Key field mapping (v1 vs old v5):
          v1: ownerAddress     → maker
          v1: price            → price (already float, NOT wei)
          v1: currency         → currency name (USDT, WBNB, BUSD)
          v1: orderHash        → order hash
          v1: id               → order id
        """
        default_currency = "ETH" if chain == "eth" else "BNB"
        maker = (item.get("ownerAddress") or "").lower()
        raw_cur = item.get("currency") or item.get("currencyName") or ""
        currency = self._resolve_currency(raw_cur, chain, default_currency)
        price = float(item.get("price") or 0)
        # v1 prices are already normalized (not in wei)
        usd_price = float(item.get("usdPrice") or 0)
        if usd_price == 0 and price > 0:
            usd_price = self.prices.to_usd(price, currency)
        order_hash = item.get("orderHash", "")
        order_id = str(item.get("id") or item.get("orderId") or "")
        order_type = item.get("orderType", 0)  # 3 = collection offer

        return {
            "maker": maker,
            "price": price,
            "currency": currency,
            "usd": usd_price,
            "order_id": order_id,
            "order_hash": order_hash,
            "order_type": order_type,
            "raw": item,
        }

    def _fetch_best_offer_maker(self, collection_address: str,
                                chain: str) -> dict:
        """Fetch the single best (highest USD) offer for a collection.

        Uses TWO data sources (as user requested):
        1. Collection page HTML → bestOffer (primary, fast)
        2. priapi/v1/nft/order/offers?nftId=X → full offer list (detailed)

        Returns {"maker": str, "price": float, "currency": str, "usd": float,
                 "_overall_best": dict, "_best_enemy": dict}.
        """
        addr = collection_address.lower()
        default_currency = "ETH" if chain == "eth" else "BNB"

        best: dict = {"maker": "", "price": 0, "currency": "", "usd": 0}
        best_enemy: dict = {"maker": "", "price": 0, "currency": "", "usd": 0}

        # ── Source 1: Collection page (bestOffer from SSR) ──
        page_data = self._resolve_collection_page_data(addr, chain)
        page_best = page_data.get("best_offer", {})
        if page_best.get("price", 0) > 0:
            log.info("  📄 page_best %s: %.4f %s ($%.4f)",
                     addr[:14], page_best["price"], page_best["currency"],
                     page_best.get("usd", 0))

        # ── Source 2: Full offer list via nftId ──
        nft_id = page_data.get("nft_id", "")
        if nft_id:
            raw_offers = self._fetch_offers_by_nftid(nft_id)
            log.info("  📊 offers_by_nftid %s: %d offers (nftId=%s)",
                     addr[:14], len(raw_offers), nft_id)

            for item in raw_offers:
                parsed = self._parse_v1_offer(item, chain)
                maker = parsed["maker"]
                price = parsed["price"]
                currency = parsed["currency"]
                usd = parsed["usd"]
                is_protected = maker in self.protected_wallets

                log.info("  📊 offer_scan %s: maker=%s price=%.6f %s ($%.4f) %s",
                         addr[:14], maker[:14] if maker else "?",
                         price, currency, usd,
                         "← OURS/FRIEND" if is_protected else "")

                if usd > best["usd"]:
                    best = {"maker": maker, "price": price,
                            "currency": currency, "usd": usd}
                if usd > best_enemy["usd"] and not is_protected:
                    best_enemy = {"maker": maker, "price": price,
                                  "currency": currency, "usd": usd}
        else:
            log.warning("  ⚠ No nftId for %s — cannot query offers API", addr[:14])
            # Fallback: use page bestOffer (no maker info though)
            if page_best.get("usd", 0) > 0:
                best = {"maker": "", "price": page_best["price"],
                        "currency": page_best["currency"],
                        "usd": page_best.get("usd", 0)}

        # --- Log results ---
        if best["maker"]:
            is_ours = best["maker"] in self.protected_wallets
            log.info("  🔍 best_offer_maker %s: OVERALL best=%s...%s ($%.4f %s) %s",
                     addr[:14],
                     best["maker"][:10], best["maker"][-4:],
                     best["usd"], best["currency"],
                     "← OUR/FRIEND" if is_ours else "← ENEMY")
        if best_enemy["maker"]:
            log.info("  🔍 best_offer_maker %s: BEST ENEMY=%s...%s ($%.4f %s)",
                     addr[:14],
                     best_enemy["maker"][:10], best_enemy["maker"][-4:],
                     best_enemy["usd"], best_enemy["currency"])

        result = dict(best_enemy) if best_enemy["maker"] else dict(best)
        result["_overall_best"] = best
        result["_best_enemy"] = best_enemy
        return result

    # _fetch_best_offers removed — replaced by _fetch_best_offer_maker above

    def _fetch_floor_price(self, collection_address: str, chain: str) -> float:
        """Fetch the floor price (cheapest listing) for a collection in USD.

        Returns 0 if no listings or on error (caller should treat 0 as "unknown").
        Caches per-scan to avoid repeated API calls.
        """
        cache_key = f"floor:{collection_address.lower()}:{chain}"
        if not hasattr(self, "_floor_cache"):
            self._floor_cache: dict[str, float] = {}
        if cache_key in self._floor_cache:
            return self._floor_cache[cache_key]

        floor_usd = 0.0
        try:
            client = self._get_okx_client()
            if client:
                resp = client.get_listings(
                    chain=chain, collection_address=collection_address, limit=5,
                )
                data_container = resp.get("data", {})
                items = data_container.get("data", []) if isinstance(data_container, dict) else []

                default_currency = "ETH" if chain == "eth" else "BNB"
                for item in (items or []):
                    raw_cur = (item.get("currencyName") or item.get("currency")
                               or item.get("currencySymbol") or "")
                    currency = self._resolve_currency(raw_cur, chain, default_currency)
                    price_raw = float(item.get("price") or 0)
                    price = self._normalize_price(price_raw, currency)
                    usd = self.prices.to_usd(price, currency)
                    if usd > 0 and (floor_usd <= 0 or usd < floor_usd):
                        floor_usd = usd
        except Exception as exc:
            log.debug("Floor price fetch failed for %s: %s",
                      collection_address[:14], exc)

        self._floor_cache[cache_key] = floor_usd
        if floor_usd > 0:
            log.info("  🏷 Floor price %s: $%.2f", collection_address[:14], floor_usd)
        return floor_usd

    def _find_our_offer(self, collection_address: str, chain: str) -> dict | None:
        """Find our highest active offer on this collection.

        Returns {"order_id": str, "price": float, "currency": str} or None.
        Uses TWO methods:
        1. Authenticated OKX API (get_my_offers) — works even without nftId
        2. priapi/v1/nft/order/offers via nftId — backup
        """
        if not self.our_wallet:
            return None

        addr = collection_address.lower()
        default_currency = "ETH" if chain == "eth" else "BNB"
        best_our: dict | None = None
        best_usd = 0.0

        def _consider(price: float, currency: str, order_id: str):
            nonlocal best_our, best_usd
            usd = self.prices.to_usd(price, currency.upper())
            if usd > best_usd:
                best_usd = usd
                best_our = {"order_id": order_id, "price": price, "currency": currency}

        # Method 1: Authenticated API (always works, no nftId needed)
        try:
            client = self._get_okx_client()
            if client:
                all_my = client.get_my_offers(chain=chain, collection_address=addr)
                for item in all_my:
                    order_id = str(item.get("orderId") or item.get("orderHash")
                                   or item.get("id") or "")
                    price_raw = item.get("price", "0")
                    # Prefer currencyAddress (contract address) over name —
                    # OKX API often omits currencyName for collection offers,
                    # causing fallback to BNB when it's actually USDT.
                    currency_raw = (item.get("currencyAddress")
                                    or item.get("currencyName")
                                    or item.get("currency")
                                    or item.get("currencySymbol") or "")
                    currency = self._resolve_currency(currency_raw, chain, default_currency)
                    try:
                        price = float(price_raw)
                    except (ValueError, TypeError):
                        price = 0.0
                    price = self._normalize_price(price, currency)
                    _consider(price, currency, order_id)
        except Exception as exc:
            log.debug("_find_our_offer API failed for %s: %s", addr[:14], exc)

        # Method 2: priapi via nftId (may find offers API missed)
        page_data = self._resolve_collection_page_data(addr, chain)
        nft_id = page_data.get("nft_id", "")
        if nft_id:
            raw_offers = self._fetch_offers_by_nftid(nft_id, max_pages=2)
            for item in raw_offers:
                parsed = self._parse_v1_offer(item, chain)
                if parsed["maker"] in self.protected_wallets:
                    _consider(parsed["price"], parsed["currency"],
                              parsed["order_id"] or parsed["order_hash"])

        if best_our:
            log.debug("_find_our_offer %s: found %.6f %s ($%.4f)",
                      addr[:14], best_our["price"], best_our["currency"], best_usd)
        return best_our

    def _find_all_our_offers(self, collection_address: str, chain: str) -> list[dict]:
        """Find ALL our active offers on this collection.

        Uses THREE methods and merges results (deduplicated by order_id).
        Method 1: Authenticated OKX API (get_my_offers)
        Method 2: Public priapi/v1/nft/order/offers via nftId
        Method 3: Local tracking fallback
        """
        addr = collection_address.lower()
        seen_ids: set[str] = set()
        results: list[dict] = []

        def _add(order_id: str, price: float, currency: str,
                 order_params: dict | None = None):
            if order_id and order_id not in seen_ids:
                seen_ids.add(order_id)
                entry: dict = {"order_id": order_id, "price": price, "currency": currency}
                if order_params:
                    entry["order_params"] = order_params
                results.append(entry)

        # Method 1: authenticated API — pass collection_address so it queries
        # both /offers AND /collection-offers for this specific collection
        api_count = 0
        try:
            client = self._get_okx_client()
            if client:
                all_my = client.get_my_offers(chain=chain,
                                               collection_address=addr)
                for item in all_my:
                    order_id = str(item.get("orderId") or item.get("orderHash")
                                   or item.get("id") or "")
                    price_raw = item.get("price", "0")
                    # Prefer currencyAddress over name — API often omits
                    # currencyName for collection offers (defaults to BNB).
                    currency_raw = (item.get("currencyAddress")
                                    or item.get("currencyName")
                                    or item.get("currency")
                                    or item.get("currencySymbol") or "")
                    currency = self._resolve_currency(currency_raw, chain,
                                                      "BNB" if chain == "bsc" else "ETH")
                    try:
                        price = float(price_raw)
                    except (ValueError, TypeError):
                        price = 0.0
                    price = self._normalize_price(price, currency)
                    # Extract protocolData.parameters for on-chain cancel
                    proto = item.get("protocolData", {})
                    params = proto.get("parameters") if isinstance(proto, dict) else None
                    _add(order_id, price, currency, order_params=params)
                    api_count += 1
        except Exception as exc:
            log.warning("get_my_offers API FAILED for %s: %s", addr[:14], exc)

        # Method 2: priapi/v1/nft/order/offers via nftId — finds offers
        # that authenticated API may miss
        priapi_count = 0
        page_data = self._resolve_collection_page_data(addr, chain)
        nft_id = page_data.get("nft_id", "")
        if nft_id:
            raw_offers = self._fetch_offers_by_nftid(nft_id, max_pages=3)
            for item in raw_offers:
                parsed = self._parse_v1_offer(item, chain)
                if parsed["maker"] in self.protected_wallets:
                    oid = parsed["order_id"] or parsed["order_hash"]
                    # Extract protocolData from raw item for on-chain cancel
                    raw = parsed.get("raw", {})
                    seaport = raw.get("okxSeaportOrderParam") or raw.get("orderData")
                    order_params = seaport.get("parameters") if isinstance(seaport, dict) else None
                    before = len(results)
                    _add(oid, parsed["price"], parsed["currency"],
                         order_params=order_params)
                    if len(results) > before:
                        priapi_count += 1

        # Method 3: local tracking fallback — if API returned nothing but
        # we KNOW we placed offers this session, report them so cancel works
        local_key = f"{addr}:{chain}"
        local_ids = self._local_placed_offers.get(local_key, [])
        local_count = 0
        if local_ids and not results:
            for oid in local_ids:
                if oid not in seen_ids:
                    seen_ids.add(oid)
                    results.append({"order_id": oid, "price": 0, "currency": ""})
                    local_count += 1
            if local_count:
                log.warning("  🔎 API returned 0 but local tracker has %d offer(s) for %s — using local",
                            local_count, addr[:14])

        if results:
            log.info("  🔎 Found %d of our offers on %s (api=%d, priapi=%d, local=%d)",
                     len(results), addr[:14], api_count, priapi_count, local_count)
        else:
            log.info("  🔎 No offers found on %s (api=%d, priapi=%d)", addr[:14], api_count, priapi_count)
        return results

    # ── Offer submission ───────────────────────────────────────

    # Currency address mapping for offer submission
    CURRENCY_ADDRESSES = {
        # BSC
        "WBNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "BNB":  "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        # BSC stables
        "USDT_BSC": "0x55d398326f99059ff775485246999027b3197955",
        "USDC_BSC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
        "BUSD": "0xe9e7cea3dedca5984780bafc599bd69add087d56",
        # ETH
        "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "ETH":  "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        # ETH stables
        "USDT_ETH": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "USDC_ETH": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "DAI":  "0x6b175474e89094c44da98b954eedeac495271d0f",
    }

    def _get_currency_address(self, currency: str, chain: str) -> str:
        """Resolve currency symbol → contract address for offer submission."""
        cur = currency.upper()
        # Try chain-specific first (USDT on BSC vs ETH)
        chain_key = f"{cur}_{chain.upper()}"
        if chain_key in self.CURRENCY_ADDRESSES:
            return self.CURRENCY_ADDRESSES[chain_key]
        # Then generic
        return self.CURRENCY_ADDRESSES.get(cur, "")

    def _cancel_existing_offer(self, addr: str, chain: str, name: str) -> bool:
        """Cancel ALL existing offers for a collection to prevent stacking.

        Returns True ONLY if:
        - No old offers exist (nothing to cancel), OR
        - ALL old offers were successfully cancelled AND verified gone.
        Returns False if any cancel failed → caller must NOT place new offer.
        """
        if self.dry_run:
            return True

        # Try finding our offers — retry once after 2s if first attempt returns empty
        # (OKX API can have propagation delays)
        all_ours = self._find_all_our_offers(addr, chain)
        if not all_ours:
            # Retry after short delay — API may not have indexed new offers yet
            time.sleep(2)
            all_ours = self._find_all_our_offers(addr, chain)

        if not all_ours:
            log.info("  🔄 %s: no existing offers to cancel (checked 2x)", name)
            return True

        client = self._get_okx_client()
        if not client:
            log.error("  🚫 %s: no OKX client — cannot cancel %d old offers", name, len(all_ours))
            return False
        ok_count = 0
        fail_count = 0
        for offer in all_ours:
            oid = offer.get("order_id", "")
            if not oid:
                fail_count += 1
                continue
            log.info("  🔄 %s: cancelling old offer %s (%.4f %s)",
                     name, oid[:12], offer["price"], offer["currency"])
            try:
                ok = client.cancel_offer(oid, chain=chain,
                                         order_params=offer.get("order_params"))
                if ok:
                    ok_count += 1
                else:
                    log.warning("  ⚠ Cancel FAILED for %s order %s", name, oid[:12])
                    fail_count += 1
                time.sleep(0.3)
            except Exception as exc:
                log.error("  Cancel error %s order %s: %s", name, oid[:12], exc)
                fail_count += 1
        log.info("  🔄 %s: cancelled %d/%d old offers (failed=%d)",
                 name, ok_count, len(all_ours), fail_count)
        # Clean up local tracker for successfully cancelled offers
        local_key = f"{addr}:{chain}"
        if local_key in self._local_placed_offers and ok_count > 0:
            cancelled_ids = {o.get("order_id", "") for o in all_ours}
            self._local_placed_offers[local_key] = [
                oid for oid in self._local_placed_offers[local_key]
                if oid not in cancelled_ids
            ]

        if fail_count > 0:
            return False

        # Post-cancel verification: wait and check that offers are actually gone.
        # BUT if ALL offers came back as "already gone" (No longer available),
        # skip verification — OKX API caches stale entries that can't be
        # cancelled but also aren't real.  Re-querying just finds the same
        # phantom records and causes an infinite ABORT loop.
        if ok_count == len(all_ours):
            # All reported as cancelled/gone — check if they were ghost offers
            # by comparing order IDs.  If the API still lists them, they're
            # stale cache entries, not real offers we need to worry about.
            # After on-chain cancel, OKX needs extra time to sync blockchain state.
            had_onchain = (
                client and hasattr(client, "_last_onchain_cancel_ts")
                and (time.time() - getattr(client, "_last_onchain_cancel_ts", 0)) < 30
            )
            verify_wait = 8 if had_onchain else 3
            if had_onchain:
                log.info("  ⏳ %s: on-chain cancel detected — waiting %ds for OKX sync", name, verify_wait)
            time.sleep(verify_wait)
            remaining = self._find_all_our_offers(addr, chain)
            old_ids = {o.get("order_id", "") for o in all_ours}
            truly_new = [r for r in remaining if r.get("order_id", "") not in old_ids]
            if truly_new:
                log.warning("  ⚠ %s: %d NEW offers appeared after cancel — ABORT",
                            name, len(truly_new))
                return False
            if remaining:
                log.info("  👻 %s: %d ghost offers still in API cache (same IDs) — ignoring, proceeding",
                         name, len(remaining))
            return True

        # Partial cancel — do strict verification
        time.sleep(3)
        remaining = self._find_all_our_offers(addr, chain)
        if remaining:
            log.warning("  ⚠ %s: %d offers STILL ACTIVE after cancel — ABORT to prevent stacking",
                        name, len(remaining))
            return False

        return True

    def _submit_undercut(self, collection_address: str, token_id: str,
                         price: float, currency: str, chain: str,
                         quantity: int = 1,
                         duration_hours: int = 720) -> bool:
        """Submit undercut offer via existing Seaport signing infrastructure.

        Supports multiple currencies: WBNB, USDT, USDC, BUSD, WETH, DAI.
        Default duration: 720 hours (30 days).
        """
        if chain == "bsc":
            return self._submit_bsc(collection_address, token_id, price, currency,
                                    quantity=quantity, duration_hours=duration_hours)
        else:
            return self._submit_eth(collection_address, token_id, price, currency,
                                    quantity=quantity, duration_hours=duration_hours)

    # ── Balance cache + low-balance alerting ──
    _balance_cache: dict[str, tuple[float, float]] = {}  # currency_addr → (balance_float, timestamp)
    _BALANCE_CACHE_TTL = 120  # refresh every 2 min
    _low_balance_alerted: set[str] = set()  # currencies we already sent TG alert for
    _LOW_BALANCE_THRESHOLDS = {  # below this → send TG warning
        "WBNB": 0.01, "USDT": 5.0, "USDC": 5.0, "BUSD": 5.0,
        "WETH": 0.005, "DAI": 5.0,
    }

    def _get_balance(self, currency_address: str) -> float:
        """Fetch on-chain ERC20 balance (cached)."""
        import time as _time
        cached = self._balance_cache.get(currency_address)
        if cached and (_time.time() - cached[1]) < self._BALANCE_CACHE_TTL:
            return cached[0]
        from okx_nft_bot.config import load_settings
        settings = load_settings()
        wallet = settings.buyer_wallet_address
        if not wallet:
            return 999999.0  # can't check → assume enough
        import json, urllib.request
        addr_padded = wallet.lower().replace('0x', '').zfill(64)
        data = '0x70a08231' + addr_padded  # balanceOf(address)
        payload = json.dumps({
            'jsonrpc': '2.0', 'method': 'eth_call',
            'params': [{'to': currency_address, 'data': data}, 'latest'], 'id': 1,
        }).encode()
        rpc_url = getattr(settings, 'sniper_rpc_url', None) or 'https://bsc-dataseed.binance.org/'
        req = urllib.request.Request(rpc_url, data=payload,
                                     headers={'Content-Type': 'application/json'})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        raw = int(resp.get('result', '0x0'), 16)
        balance = raw / (10 ** 18)
        self._balance_cache[currency_address] = (balance, _time.time())
        return balance

    def _send_low_balance_tg(self, currency: str, balance: float, threshold: float):
        """Send Telegram alert when balance drops below threshold."""
        try:
            import json, urllib.request, os
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if not bot_token or not chat_id:
                return
            msg = (
                f"⚠️ <b>Low {currency} balance!</b>\n"
                f"Balance: <code>{balance:.6f}</code>\n"
                f"Threshold: <code>{threshold:.6f}</code>\n"
                f"Top up to keep placing offers."
            )
            payload = json.dumps({
                "chat_id": chat_id, "text": msg, "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            log.info("📱 TG alert sent: %s balance=%.6f < threshold=%.6f", currency, balance, threshold)
        except Exception as exc:
            log.debug("TG low-balance alert failed: %s", exc)

    def _check_balance_for_offer(self, currency_address: str, currency: str,
                                  offer_price: float) -> bool:
        """Pre-flight balance check for a single offer.

        - Skip if balance < offer price (can't cover if someone accepts)
        - Send TG alert if balance drops below low threshold
        - No permanent halts — each offer checked individually
        """
        try:
            balance = self._get_balance(currency_address)
            cur = currency.upper()

            # ── Low balance TG alert (once per currency until recovered) ──
            threshold = self._LOW_BALANCE_THRESHOLDS.get(cur, 1.0)
            if balance < threshold:
                if cur not in self._low_balance_alerted:
                    self._low_balance_alerted.add(cur)
                    self._send_low_balance_tg(cur, balance, threshold)
                    log.warning("⚠️ %s balance %.6f below threshold %.6f — TG alert sent",
                                cur, balance, threshold)
            else:
                # Balance recovered — allow future alerts
                self._low_balance_alerted.discard(cur)

            # ── Skip if can't cover even 1 acceptance ──
            if balance < offer_price:
                log.warning("⛔ %s balance=%.6f < offer=%.6f — SKIP (can't cover acceptance)",
                            cur, balance, offer_price)
                return False
            return True
        except Exception as exc:
            log.debug("Balance check failed (proceeding anyway): %s", exc)
            return True

    def _submit_bsc(self, collection_address: str, token_id: str,
                    price: float, currency: str = "WBNB",
                    quantity: int = 1, duration_hours: int = 720) -> bool:
        """Submit BSC offers through the governed execution path."""
        try:
            engine = self._get_bsc_engine()
            if engine is None:
                self._record_execution_submit_event(
                    chain="bsc",
                    collection=collection_address,
                    price_bnb=price,
                    status="failed",
                    reason="mass_offer_engine_unavailable",
                )
                return False

            currency_address = self._get_currency_address(currency, "bsc")
            if not currency_address:
                log.error("BSC offer: unknown currency %s", currency)
                self._record_execution_submit_event(
                    chain="bsc",
                    collection=collection_address,
                    price_bnb=price,
                    status="failed",
                    reason=f"unknown_currency:{currency}",
                )
                return False

            cur_upper = currency.upper()
            if not self._check_balance_for_offer(currency_address, cur_upper, price):
                self._record_execution_submit_event(
                    chain="bsc",
                    collection=collection_address,
                    price_bnb=price,
                    status="blocked",
                    reason=f"insufficient_balance:{cur_upper}",
                )
                return False

            if engine.governor.effective_dry_run(False):
                reason = "execution_dry_run_enabled"
                log.warning(
                    "BSC governed submit BLOCKED %s token=%s price=%.6f %s: %s",
                    collection_address[:14], token_id or "col", price, currency, reason,
                )
                self._record_execution_submit_event(
                    chain="bsc",
                    collection=collection_address,
                    price_bnb=price,
                    status="blocked",
                    reason=reason,
                )
                return False

            ok = engine.place_single_offer(
                collection_address=collection_address,
                token_id=token_id,
                price_wbnb=price,
                currency_address=currency_address,
                chain="bsc",
                duration_hours=duration_hours,
                dry_run=False,
                quantity=quantity,
            )
            if ok:
                key = f"{collection_address.lower()}:bsc"
                try:
                    active = engine.state.get_active_offers(chain="bsc")
                    matches = [offer.order_hash for offer in active if offer.collection == collection_address.lower()]
                    if matches:
                        self._local_placed_offers.setdefault(key, []).append(matches[-1])
                except Exception as exc:
                    log.debug("BSC local offer tracking refresh failed: %s", exc)
            else:
                self._record_execution_submit_event(
                    chain="bsc",
                    collection=collection_address,
                    price_bnb=price,
                    status="failed",
                    reason="governed_submit_failed",
                )
            return ok
        except Exception as exc:
            log.error("BSC governed offer submit failed (%s): %s", currency, exc, exc_info=True)
            self._record_execution_submit_event(
                chain="bsc",
                collection=collection_address,
                price_bnb=price,
                status="failed",
                reason=str(exc),
            )
            return False

    def _get_bsc_engine(self):
        """Lazy-init and cache MassOfferEngine for BSC submissions."""
        if hasattr(self, "_bsc_engine") and self._bsc_engine is not None:
            return self._bsc_engine
        try:
            from okx_nft_bot.mass_offer.engine import MassOfferEngine
            from okx_nft_bot.config import load_settings
            settings = load_settings()
            self._bsc_engine = MassOfferEngine(settings=settings)
            log.info("BSC MassOfferEngine initialized OK (db=%s)", settings.execution_db_path)
            return self._bsc_engine
        except Exception as exc:
            log.error("BSC MassOfferEngine init failed: %s", exc, exc_info=True)
            return None

    def _submit_eth(self, collection_address: str, token_id: str,
                    price: float, currency: str = "WETH",
                    quantity: int = 1, duration_hours: int = 720) -> bool:
        """Block live ETH offers until an execution-governed ETH runtime exists."""
        _ = token_id, quantity, duration_hours
        reason = "eth_live_submit_disabled_until_governed_runtime"
        log.warning(
            "ETH governed submit BLOCKED %s token=%s price=%.6f %s: %s",
            collection_address[:14], token_id or "col", price, currency, reason,
        )
        self._record_execution_submit_event(
            chain="eth",
            collection=collection_address,
            price_bnb=price,
            status="blocked",
            reason=reason,
        )
        return False

    # ── Data conversion helpers ────────────────────────────────

    def _normalized_to_parasite(self, o, chain: str) -> ParasiteOffer:
        """Convert NormalizedOffer → ParasiteOffer."""
        addr = (o.collection_slug_or_address or "").lower()
        wl_info = self._wl_index.get(addr, {})
        name = wl_info.get("collection_name") or wl_info.get("name") or addr[:14]

        default_currency = "ETH" if chain == "eth" else "BNB" if chain == "bsc" else "ETH"

        # Currency first (needed for price normalization)
        currency = self._resolve_currency(o.currency, chain, default_currency)

        # Price handling: NormalizedOffer stores raw price which may be in wei
        price = float(o.price or 0)
        price = self._normalize_price(price, currency)

        log.debug(
            "🔍 Normalized offer: %s token=%s price=%.8f currency=%s (raw_price=%s raw_cur=%s)",
            name, o.token_id, price, currency, o.price, o.currency
        )

        maker = ""
        if hasattr(o, "maker"):
            maker = (o.maker or "").lower()
        elif hasattr(o, "maker_address"):
            maker = (o.maker_address or "").lower()

        return ParasiteOffer(
            collection_address=addr,
            collection_name=name,
            chain=chain,
            token_id=o.token_id or "",
            price=price,
            currency=currency,
            offer_id=o.offer_id or "",
            source_type=o.source_type or "token_offer",
            maker=maker,
            expires_at=str(o.expires_at or ""),
        )

    @staticmethod
    def _normalize_price(price: float, currency: str) -> float:
        """Smart wei→human conversion.

        NFT offer prices are never realistically > 10,000 in any currency.
        If price is absurdly large, it's in wei (18 decimals) or
        token smallest unit (6 decimals for USDC/USDT on some chains).

        Threshold: any price > 10_000 is almost certainly NOT human-readable.
        Real NFT offers are under $10k in any currency.
        """
        if price <= 0:
            return price

        # Already sane (human-readable range)
        if price <= 10_000:
            return price

        # Price > 10k — definitely in smallest unit, try to convert
        # Try 18 decimals first (ETH/BNB/WETH/WBNB)
        p18 = price / 1e18
        if p18 >= 0.0000001:  # at least 0.1 gwei equivalent — plausible
            return p18

        # Try 6 decimals (USDT/USDC)
        if currency.upper() in ("USDT", "USDC", "BUSD"):
            p6 = price / 1e6
            if p6 >= 0.0001:
                return p6

        # Try 8 decimals
        p8 = price / 1e8
        if p8 >= 0.0001:
            return p8

        # Fallback: 18 decimals even if tiny
        return p18

    @staticmethod
    def _resolve_currency(raw_currency: str | None, chain: str,
                          default: str) -> str:
        """Resolve currency address or symbol to human-readable symbol."""
        if not raw_currency:
            return default

        c = raw_currency.strip().lower()

        # Already a symbol
        if len(c) <= 10 and not c.startswith("0x"):
            return raw_currency.upper()

        # Known token addresses
        KNOWN = {
            # ETH mainnet
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
            "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
            "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
            # BSC
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
            "0x55d398326f99059ff775485246999027b3197955": "USDT",
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
            "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
            "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": "DAI",
            # Zero address = native
            "0x0000000000000000000000000000000000000000": default,
        }
        return KNOWN.get(c, default)

    def _raw_to_parasite(self, raw: dict, chain: str) -> ParasiteOffer:
        """Convert raw API dict → ParasiteOffer."""
        addr = (raw.get("collectionAddress") or raw.get("collection_address")
                or raw.get("contractAddress") or "").lower()
        wl_info = self._wl_index.get(addr, {})
        name = (raw.get("collectionName") or raw.get("projectName")
                or raw.get("slug") or raw.get("name")
                or wl_info.get("collection_name") or wl_info.get("name")
                or addr[:14])

        # Extract and cache OKX project ID if present in raw offer data
        for pid_key in ("project", "projectId", "nftProjectId", "collectionId"):
            pid_val = raw.get(pid_key)
            if pid_val and str(pid_val).isdigit() and str(pid_val) != "0" and addr:
                try:
                    from okx_nft_bot.counterbid.okx_api import OKXAPIClient
                    OKXAPIClient.register_project_id(addr, chain, pid_val)
                except Exception:
                    pass
                break

        price = 0.0
        try:
            price = float(raw.get("price", 0))
        except (TypeError, ValueError):
            pass

        default_currency = "ETH" if chain == "eth" else "BNB" if chain == "bsc" else "ETH"
        raw_cur = raw.get("currencyAddress") or raw.get("currency") or ""
        currency = self._resolve_currency(raw_cur, chain, default_currency)

        # Smart wei → human-readable conversion
        price = self._normalize_price(price, currency)

        log.debug(
            "🔍 Raw offer: %s token=%s price=%.8f currency=%s (raw: price=%s cur=%s)",
            name, raw.get("tokenId"), price, currency, raw.get("price"), raw_cur
        )

        maker = (raw.get("maker") or raw.get("makerAddress")
                 or raw.get("ownerAddress") or "").lower()

        return ParasiteOffer(
            collection_address=addr,
            collection_name=name,
            chain=chain,
            token_id=str(raw.get("tokenId") or ""),
            price=price,
            currency=currency,
            offer_id=str(raw.get("orderId") or raw.get("orderHash") or ""),
            source_type="token_offer" if raw.get("tokenId") else "collection_offer",
            maker=maker,
            expires_at=str(raw.get("expirationTime", "")),
        )

    # ── Config helpers ─────────────────────────────────────────

    def _get_max_price(self, collection_address: str, chain: str,
                       offer_currency: str = "") -> float | None:
        """Get max offer price from buy_config.

        Only returns a cap if there's a SPECIFIC config for this collection.
        Default chain-level caps are too generic and cause wrong-currency capping
        (e.g. BNB cap applied to USDT offer).
        """
        addr = collection_address.lower()
        collections = self.buy_config.get("collections", {})
        if addr in collections:
            entry = collections[addr]
            if not entry.get("enabled", True):
                return None
            max_p = entry.get("max_offer_price") or entry.get("max_buy_price")
            if max_p:
                # If currencies differ, convert max_price to offer currency
                cfg_cur = (entry.get("currency") or "").upper()
                if cfg_cur and offer_currency and cfg_cur != offer_currency:
                    cfg_usd = self.prices.to_usd(max_p, cfg_cur)
                    converted = self.prices.from_usd(cfg_usd, offer_currency)
                    log.debug("  max_price converted: %.4f %s → %.6f %s",
                              max_p, cfg_cur, converted, offer_currency)
                    return converted
                return max_p
        # No specific config → no cap (don't use chain defaults for parasite hunter)
        return None

    # ── Phase 4: Parasite Intel ─────────────────────────────────

    # Known marketplace contracts for approval checks
    _MARKETPLACE_CONTRACTS = {
        "bsc": {
            "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "Seaport 1.5",
            "0x00000000000001ad428e4906ae43d8f9852d0dd6": "Seaport 1.6",
            "0xb4a437cae9a15cde291780c65a3cf8bbe7252fcc": "Element",
        },
        "eth": {
            "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "Seaport 1.5",
            "0x00000000000001ad428e4906ae43d8f9852d0dd6": "Seaport 1.6",
            "0x000000000000ad05ccc4f10045630fb830b95127": "Blur",
        }
    }
    _CURRENCY_TOKENS = {
        "bsc": {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18),
            "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
            "0xe9e7cea3dedca5984780bafc599bd69add087d56": ("BUSD", 18),
        },
        "eth": {
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
            "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
        }
    }

    def _run_parasite_intel(self, chain: str) -> dict:
        """Phase 4: Gather intelligence on all parasite wallets.

        Checks:
        - Token balances (how much $ each parasite has)
        - Active OKX offers count per parasite
        - Sends Telegram alert with top parasites + their capital

        Returns summary dict.
        """
        import json as _json
        import urllib.request as _req

        chain_tokens = self._CURRENCY_TOKENS.get(chain, {})
        rpc_url = getattr(self, '_settings', None)
        if rpc_url:
            rpc_url = getattr(rpc_url, 'sniper_rpc_url', None)
        if not rpc_url:
            rpc_url = {
                "bsc": "https://bsc-dataseed.binance.org/",
                "eth": "https://eth.llamarpc.com",
            }.get(chain, "https://bsc-dataseed.binance.org/")

        intel = {
            "with_balance": 0,
            "with_offers": 0,
            "parasites": [],
        }

        for wallet in sorted(self.target_wallets):
            balances = {}

            # Check token balances via RPC
            for token_addr, (token_name, decimals) in chain_tokens.items():
                try:
                    addr_padded = wallet.lower().replace('0x', '').zfill(64)
                    call_data = '0x70a08231' + addr_padded
                    payload = _json.dumps({
                        'jsonrpc': '2.0', 'method': 'eth_call',
                        'params': [{'to': token_addr, 'data': call_data}, 'latest'], 'id': 1,
                    }).encode()
                    req = _req.Request(rpc_url, data=payload,
                                       headers={'Content-Type': 'application/json'})
                    resp = _json.loads(_req.urlopen(req, timeout=5).read())
                    raw = int(resp.get('result', '0x0'), 16)
                    balance = raw / (10 ** decimals)
                    if balance > 0.001:
                        balances[token_name] = round(balance, 6)
                except Exception:
                    pass

            # Native balance
            try:
                payload = _json.dumps({
                    'jsonrpc': '2.0', 'method': 'eth_getBalance',
                    'params': [wallet, 'latest'], 'id': 1,
                }).encode()
                req = _req.Request(rpc_url, data=payload,
                                   headers={'Content-Type': 'application/json'})
                resp = _json.loads(_req.urlopen(req, timeout=5).read())
                raw = int(resp.get('result', '0x0'), 16)
                native = raw / (10 ** 18)
                native_name = "BNB" if chain == "bsc" else "ETH"
                if native > 0.001:
                    balances[native_name] = round(native, 6)
            except Exception:
                pass

            # Count active OKX offers (via authenticated API)
            # NOTE: priapi/v5/nft/ec/ endpoints are dead (404).
            # Use authenticated API for wallet offer count.
            offer_count = 0
            try:
                client = self._get_okx_client()
                if client:
                    my_offers = client.get_my_offers(chain=chain)
                    # Filter to this wallet's offers
                    offer_count = sum(
                        1 for o in my_offers
                        if (o.get("maker") or o.get("makerAddress") or "").lower() == wallet
                    )
            except Exception:
                pass

            if balances or offer_count > 0:
                p_info = {
                    "wallet": wallet,
                    "balances": balances,
                    "offers": offer_count,
                }
                intel["parasites"].append(p_info)
                if balances:
                    intel["with_balance"] += 1
                if offer_count > 0:
                    intel["with_offers"] += 1

                bal_str = " ".join(f"{v}{k}" for k, v in balances.items())
                log.info("  🕷 %s...%s: %s | %d offers",
                         wallet[:10], wallet[-4:], bal_str or "no balance", offer_count)

            time.sleep(0.1)  # RPC rate limit

        # Send Telegram intel report
        if intel["parasites"]:
            self._send_parasite_intel_tg(chain, intel)

        return intel

    def _send_parasite_intel_tg(self, chain: str, intel: dict):
        """Send parasite intel summary to Telegram."""
        try:
            import json as _json, urllib.request as _req, os
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if not bot_token or not chat_id:
                return

            lines = [f"🕷 <b>Parasite Intel — {chain.upper()}</b>\n"]
            lines.append(f"С балансом: {intel['with_balance']}/{len(self.target_wallets)}")
            lines.append(f"С офферами: {intel['with_offers']}/{len(self.target_wallets)}\n")

            for p in sorted(intel["parasites"], key=lambda x: x["offers"], reverse=True)[:10]:
                w = p["wallet"]
                bal = " ".join(f"{v}{k}" for k, v in p["balances"].items()) or "—"
                lines.append(f"<code>{w[:8]}...{w[-4:]}</code> | {p['offers']} offers | {bal}")

            msg = "\n".join(lines)
            payload = _json.dumps({
                "chat_id": chat_id, "text": msg, "parse_mode": "HTML",
            }).encode()
            req = _req.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"})
            _req.urlopen(req, timeout=5)
            log.info("📱 Parasite intel TG alert sent")
        except Exception as exc:
            log.debug("Parasite intel TG failed: %s", exc)

    # ── Phase 3: Sell side ─────────────────────────────────────

    def _run_sell_phase(self, chain: str) -> int:
        """Scan our wallet NFTs, list them at lowest_listing - step but above low_price.

        Returns number of listings placed.
        """
        client = self._get_okx_client()
        if not client:
            from okx_nft_bot.config import load_settings
            from okx_nft_bot.counterbid.okx_api import OKXAPIClient
            settings = load_settings()
            client = OKXAPIClient(settings=settings)

        wallet = self.our_wallet
        if not wallet:
            log.warning("SELL PHASE: no wallet address configured")
            return 0

        collections_cfg = self.buy_config.get("collections", {})
        sell_bps = self.buy_config.get("sell_settings", {}).get("undercut_step_bps", 50)
        placed = 0

        # Get our NFT inventory
        all_nfts = []
        cursor = None
        for _ in range(10):
            try:
                resp = client.get_wallet_nfts(
                    chain=chain,
                    wallet_address=wallet,
                    limit=50,
                    cursor=cursor,
                )
                data = resp.get("data", {})
                items = data.get("data", []) if isinstance(data, dict) else []
                if not items:
                    break
                all_nfts.extend(items)
                cursor = data.get("cursor")
                if not cursor:
                    break
                time.sleep(0.3)
            except Exception as e:
                log.warning("SELL PHASE: wallet NFT fetch error: %s", e)
                break

        if not all_nfts:
            log.info("SELL PHASE %s: no NFTs in wallet", chain.upper())
            return 0

        log.info("SELL PHASE %s: found %d NFTs in wallet", chain.upper(), len(all_nfts))

        # DEBUG: dump first NFT keys so we know what the API actually returns
        if all_nfts:
            sample = all_nfts[0]
            log.info("SELL DEBUG: first NFT keys=%s", list(sample.keys()))
            log.info("SELL DEBUG: first NFT sample=%s",
                     {k: (str(v)[:80] if not isinstance(v, dict) else str(v)[:120]) for k, v in sample.items()})

        # Group NFTs by collection
        # OKX asset-list nests contract address in assetContract.contractAddress
        nfts_by_collection: dict[str, list[dict]] = defaultdict(list)
        dropped_count = 0
        for nft in all_nfts:
            # Primary: nested assetContract.contractAddress (OKX asset-list format)
            asset_contract = nft.get("assetContract") or {}
            col_addr = (
                asset_contract.get("contractAddress")
                or nft.get("collectionAddress")
                or nft.get("tokenContractAddress")
                or nft.get("contractAddress")
                or ""
            ).lower()
            if col_addr:
                nfts_by_collection[col_addr].append(nft)
            else:
                dropped_count += 1
                if dropped_count <= 3:
                    log.warning("SELL PHASE: NFT without collection addr: keys=%s", list(nft.keys()))

        if dropped_count:
            log.warning("SELL PHASE %s: %d/%d NFTs DROPPED (no collection address field found!)",
                        chain.upper(), dropped_count, len(all_nfts))

        # Default prices from config (used when collection has no specific config)
        chain_defaults = self.buy_config.get("defaults", {}).get(chain, {})
        default_max_op = chain_defaults.get("max_offer_price", 0)
        default_currency = chain_defaults.get("currency", "BNB" if chain == "bsc" else "ETH")

        log.info("SELL PHASE %s: %d unique collections in wallet (default max=%.4f %s)",
                 chain.upper(), len(nfts_by_collection), default_max_op, default_currency)
        for col_addr, nfts in nfts_by_collection.items():
            cfg = collections_cfg.get(col_addr, {})
            low_price = cfg.get("low_price")
            if not low_price:
                # Auto-calculate: low_price = max_offer_price * 1.1
                max_op = cfg.get("max_offer_price", 0)
                if not max_op or max_op <= 0:
                    # No collection-specific config — use chain defaults
                    max_op = default_max_op
                if max_op and max_op > 0:
                    low_price = round(max_op * 1.1, 6)
                else:
                    wl_info = self._wl_index.get(col_addr, {})
                    coll_name = cfg.get("name") or wl_info.get("collection_name") or col_addr[:14]
                    log.info("  SELL SKIP %s (%s): no low_price/max_offer_price in config (%d NFTs)",
                             col_addr[:14], coll_name, len(nfts))
                    continue  # No max_offer_price either → skip selling

            sell_currency = cfg.get("currency", "WBNB" if chain == "bsc" else "WETH")
            currency_address = self._get_currency_address(sell_currency, chain)
            coll_name = cfg.get("name") or self._wl_index.get(col_addr, {}).get("collection_name") or col_addr[:14]
            log.info("  SELL PROCESS %s (%s): %d NFTs, low_price=%.4f %s",
                     col_addr[:14], coll_name, len(nfts), low_price, sell_currency)

            # Decimals for this currency
            sell_decimals = 18
            if sell_currency.upper() in ("USDT", "USDC") and chain == "eth":
                sell_decimals = 6

            # Get current listings for this collection
            try:
                listings_resp = client.get_listings(
                    chain=chain,
                    collection_address=col_addr,
                    limit=50,
                )
                listings_data = listings_resp.get("data", {})
                listings = listings_data.get("data", []) if isinstance(listings_data, dict) else []
            except Exception as e:
                log.warning("SELL PHASE: listings fetch error for %s: %s", col_addr[:14], e)
                continue

            # Separate our listings vs enemy listings
            our_listings: list[dict] = []
            cheapest_enemy_price = None
            for listing in listings:
                seller = (listing.get("makerAddress") or listing.get("maker") or "").lower()
                price_raw = listing.get("price", "0")
                try:
                    lprice = int(price_raw) / (10 ** sell_decimals) if price_raw.isdigit() else float(price_raw)
                except (ValueError, TypeError, ArithmeticError):
                    continue
                if lprice <= 0:
                    continue

                if seller == wallet:
                    our_listings.append({
                        "order_id": listing.get("orderId") or listing.get("orderHash") or listing.get("id", ""),
                        "token_id": listing.get("tokenId", ""),
                        "price": lprice,
                    })
                else:
                    if cheapest_enemy_price is None or lprice < cheapest_enemy_price:
                        cheapest_enemy_price = lprice

            # Determine our target sell price
            if cheapest_enemy_price is None:
                # No enemy listings → list at low_price
                our_sell_price = low_price
                log.info("  SELL %s: no enemy listings → price=%.4f (low_price)",
                         col_addr[:14], our_sell_price)
            else:
                # Undercut cheapest enemy by step
                step_factor = 1 - sell_bps / 10000
                our_sell_price = cheapest_enemy_price * step_factor
                # Floor at low_price
                if our_sell_price < low_price:
                    our_sell_price = low_price
                log.info("  SELL %s: enemy_cheapest=%.4f → our=%.4f (floor=%.4f)",
                         col_addr[:14], cheapest_enemy_price, our_sell_price, low_price)

            # ── Re-list: cancel our listings that are MORE expensive than target ──
            our_listed_token_ids = set()
            for ol in our_listings:
                our_listed_token_ids.add(ol["token_id"])
                if cheapest_enemy_price is not None and ol["price"] > cheapest_enemy_price:
                    # Enemy undercut us! Cancel and re-list
                    log.info("  SELL RE-LIST %s #%s: our=%.4f > enemy=%.4f → cancel & re-list at %.4f",
                             col_addr[:14], ol["token_id"], ol["price"],
                             cheapest_enemy_price, our_sell_price)
                    if not self.dry_run:
                        try:
                            client.cancel_listing(ol["order_id"])
                        except Exception as exc:
                            log.error("  SELL CANCEL ERROR %s #%s: %s", col_addr[:14], ol["token_id"], exc)
                            continue
                        time.sleep(0.5)
                    # Will be re-listed below as if unlisted
                    our_listed_token_ids.discard(ol["token_id"])
                elif ol["price"] <= our_sell_price:
                    # Our listing is already cheapest or at/below target — keep it
                    log.debug("  SELL %s #%s: already listed at %.4f ≤ target %.4f — keep",
                              col_addr[:14], ol["token_id"], ol["price"], our_sell_price)

            # ── List unlisted NFTs + re-list cancelled ones ──
            for nft in nfts:
                token_id = nft.get("tokenId") or nft.get("token_id", "")
                if not token_id:
                    continue
                if token_id in our_listed_token_ids:
                    continue  # Already listed at good price

                if self.dry_run:
                    log.info("  SELL DRY-RUN: %s #%s at %.4f %s",
                             col_addr[:14], token_id, our_sell_price, sell_currency)
                    placed += 1
                    continue

                try:
                    price_raw = str(int(our_sell_price * (10 ** sell_decimals)))
                    valid_time = int(time.time()) + 86400 * 7  # 7 days

                    result = client.create_listing(
                        chain=chain,
                        wallet_address=wallet,
                        collection_address=col_addr,
                        token_id=token_id,
                        price_raw=price_raw,
                        currency_address=currency_address,
                        valid_time=valid_time,
                    )
                    if result.get("order_id"):
                        placed += 1
                        log.info("  SELL OK: %s #%s at %.4f %s → order=%s",
                                 col_addr[:14], token_id, our_sell_price,
                                 sell_currency, result["order_id"])
                        self._tg_send(
                            f"📤 <b>SELL</b> {cfg.get('name', col_addr[:14])} #{token_id}\n"
                            f"Price: {our_sell_price:.4f} {sell_currency}\n"
                            f"Floor: {low_price} | Chain: {chain.upper()}"
                        )
                    else:
                        log.warning("  SELL FAILED: %s #%s — no order_id", col_addr[:14], token_id)
                except Exception as exc:
                    log.error("  SELL ERROR: %s #%s: %s", col_addr[:14], token_id, exc)

                if self.delay > 0:
                    time.sleep(self.delay)

        return placed

    # ── Telegram alerts ────────────────────────────────────────

    def _tg_send(self, msg: str):
        """Send Telegram message."""
        if not self.tg_bot_token or not self.tg_chat_id:
            return
        try:
            from curl_cffi import requests as http
        except ImportError:
            import requests as http  # type: ignore
        try:
            http.post(
                f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage",
                json={
                    "chat_id": self.tg_chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception:
            pass

    def _alert_undercut(self, collection_name: str, token_id: str,
                        chain: str, parasite_price: float, our_price: float,
                        parasite_cur: str, our_cur: str,
                        parasite_usd: float, our_usd: float,
                        dry: bool = False,
                        collection_address: str = ""):
        dry_tag = " [DRY]" if dry else ""

        # Use collection_address directly for OKX link (no reverse lookup needed)
        addr = collection_address or self._wl_name_to_addr.get(collection_name.lower(), "")
        okx_link = ""
        if addr:
            okx_link = (
                f"\n<a href=\"https://web3.okx.com/nft/collection/"
                f"{chain}/{addr}\">🔗 OKX</a>"
            )

        # If collection_name is just an address fragment, try to resolve it
        display_name = collection_name
        if display_name.startswith("0x") and len(display_name) <= 14:
            # Try WL index first, then collections_registry
            full_addr = addr or display_name
            wl_info = self._wl_index.get(full_addr, {})
            resolved = wl_info.get("collection_name") or wl_info.get("name")
            if resolved:
                display_name = resolved
            else:
                # Show shortened address clearly
                display_name = f"{full_addr[:8]}…{full_addr[-4:]}" if len(full_addr) > 14 else full_addr

        # Currency display
        if parasite_cur == our_cur:
            price_line = (
                f"Паразит: {parasite_price:.4f} {parasite_cur} (${parasite_usd:.2f})\n"
                f"Наш: {our_price:.4f} {our_cur} (${our_usd:.2f})"
            )
        else:
            price_line = (
                f"Паразит: {parasite_price:.4f} {parasite_cur} (${parasite_usd:.2f})\n"
                f"Наш: {our_price:.4f} {our_cur} (${our_usd:.2f}) ⚡️конверт"
            )

        msg = (
            f"🎯 {display_name} #{token_id}\n"
            f"{price_line}\n"
            f"{chain.upper()}{dry_tag}{okx_link}"
        )
        self._tg_send(msg)

    def _alert_scan_report(self, report: ScanReport):
        """Send scan summary to Telegram."""
        if report.total_offers_found == 0:
            msg = (
                f"🎯 Скан v4 — чисто\n"
                f"Чейны: {', '.join(c.upper() for c in report.chains_scanned)}\n"
                f"Паразитов: {len(self.target_wallets)} | Друзей: {len(self.friend_wallets)}\n"
                f"Время: {report.scan_duration_sec:.1f}с | #{self.total_scans}"
            )
            self._tg_send(msg)
            return

        mode = "[DRY]" if self.dry_run else "[LIVE]"

        # Build collection list with USD values
        coll_lines = []
        total_usd = 0.0
        sorted_colls = sorted(
            report.by_collection.items(),
            key=lambda x: len(x[1]),
            reverse=True,
        )
        for addr, offers in sorted_colls[:15]:
            name = offers[0].collection_name if offers else addr[:14]
            coll_usd = sum(self.prices.to_usd(o.price, o.currency) for o in offers)
            total_usd += coll_usd
            is_wl = "WL" if addr in self._wl_index else "  "
            coll_lines.append(
                f"  {is_wl} {name}: {len(offers)} офф (${coll_usd:.2f})"
            )
        for addr, offers in sorted_colls[15:]:
            total_usd += sum(self.prices.to_usd(o.price, o.currency) for o in offers)
        if len(sorted_colls) > 15:
            coll_lines.append(f"  ... и ещё {len(sorted_colls) - 15}")

        bnb = self.prices.get_usd_price("BNB")
        eth = self.prices.get_usd_price("ETH")

        msg = (
            f"🎯 СКАН v4 {mode}\n"
            f"{'─' * 22}\n"
            f"📋 Фаза 1 — WL захват:\n"
            f"  Коллекций: {report.wl_collections}\n"
            f"  Офферов: {report.wl_offers_found}\n"
            f"  Перебито: {report.wl_undercuts_placed}\n"
            f"  ✅ Уже лучшие: {report.wl_already_best}\n"
            f"  🤝 Друзья/свои: {self._friend_skipped_count}\n"
            f"{'─' * 22}\n"
            f"🕷 Фаза 2 — non-WL паразиты:\n"
            f"  Коллекций: {report.nonwl_collections}\n"
            f"  Офферов: {report.nonwl_offers_found}\n"
            f"  Перебито: {report.nonwl_undercuts_placed}\n"
            f"{'─' * 22}\n"
            + "\n".join(coll_lines) + "\n"
            f"{'─' * 22}\n"
            f"💰 BNB=${bnb:.0f} ETH=${eth:.0f}\n"
            f"Паразитов: {len(self.target_wallets)} | Друзей: {len(self.friend_wallets)}\n"
            f"Макс WL: ${self.max_usd:.2f} | non-WL: ${self.nonwl_max_usd:.4f}\n"
            f"Валюты: {','.join(self.offer_currencies)}\n"
            f"Чейны: {', '.join(c.upper() for c in report.chains_scanned)}\n"
            f"Время: {report.scan_duration_sec:.1f}с | #{self.total_scans}"
        )
        self._tg_send(msg)

    # ── Dashboard / status ─────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return current hunter status for API/dashboard."""
        report = self.last_report
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "target_wallets": len(self.target_wallets),
            "chains": self.chains,
            "total_scans": self.total_scans,
            "already_winning": self.already_winning,
            "scan_interval": self.scan_interval,
            "undercut_bps": self.undercut_bps,
            "last_scan": {
                "collections": report.collections_with_offers if report else 0,
                "offers_found": report.total_offers_found if report else 0,
                "undercuts_placed": report.undercuts_placed if report else 0,
                "duration_sec": report.scan_duration_sec if report else 0,
                "collections_detail": {
                    addr: [
                        {
                            "name": o.collection_name,
                            "token_id": o.token_id,
                            "price": o.price,
                            "currency": o.currency,
                        }
                        for o in offers
                    ]
                    for addr, offers in (report.by_collection if report else {}).items()
                },
            } if report else None,
        }
