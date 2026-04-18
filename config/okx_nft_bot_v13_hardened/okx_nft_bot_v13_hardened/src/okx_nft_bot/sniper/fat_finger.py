"""
Fat Finger Sniper v2 — catch mispriced listings before anyone else.

Architecture:
    1. OpenSea Stream API (WebSocket) — realtime listing events, <100ms latency
    2. Floor price cache — updated every 30s from OKX/OpenSea APIs
    3. Anomaly detector — flags listings at <X% of floor price
    4. Instant buyer — executes purchase via web3.py + Seaport fulfillOrder
    5. Optional: Flashbots/bloXroute for MEV-protected tx submission

Flow:
    [OpenSea WS] → new listing event
        → compare price vs floor cache
        → if price < floor * threshold
            → build fulfillOrder tx
            → sign with private key
            → submit via RPC (or MEV bundle)
            → alert Telegram

Env vars:
    SNIPER_ENABLED=1
    SNIPER_THRESHOLD=0.5          # buy if listing < 50% of floor
    SNIPER_MAX_BUY_ETH=0.5        # max spend per buy
    SNIPER_COLLECTIONS=0x...,0x.. # whitelist of collections to snipe (empty = all)
    SNIPER_RPC_URL=https://...    # fast RPC endpoint
    BUYER_WALLET_ADDRESS=0x...
    BUYER_WALLET_PRIVATE_KEY=0x...
    OPENSEA_API_KEY=...           # for Stream API access
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("sniper.fat_finger")

# OpenSea Stream API endpoint
OPENSEA_STREAM_URL = "wss://stream.openseabeta.com/socket/websocket"


@dataclass
class FloorCache:
    """In-memory floor price cache per collection."""
    floors: dict[str, float] = field(default_factory=dict)  # collection_addr -> floor ETH
    updated_at: dict[str, float] = field(default_factory=dict)  # collection_addr -> timestamp
    ttl: float = 30.0  # seconds

    def get(self, collection: str) -> float | None:
        collection = collection.lower()
        if collection not in self.floors:
            return None
        if time.time() - self.updated_at.get(collection, 0) > self.ttl:
            return None  # stale
        return self.floors[collection]

    def set(self, collection: str, floor: float):
        collection = collection.lower()
        self.floors[collection] = floor
        self.updated_at[collection] = time.time()


@dataclass
class SniperHit:
    """A detected fat finger listing."""
    collection_address: str
    collection_name: str
    token_id: str
    listing_price: float
    floor_price: float
    discount_pct: float  # e.g. 0.95 = 95% discount
    currency: str
    chain: str
    order_hash: str
    seller: str
    detected_at: str  # ISO timestamp
    bought: bool = False
    tx_hash: str | None = None
    error: str | None = None


class FatFingerSniper:
    """Realtime fat finger detection and sniping engine.

    Phase 1 (current): Detection + Telegram alerts
    Phase 2 (next): Auto-buy via web3.py + Seaport
    Phase 3 (future): MEV bundles for guaranteed execution
    """

    def __init__(self):
        self.enabled = os.getenv("SNIPER_ENABLED", "0").lower() in ("1", "true", "yes")
        self.threshold = float(os.getenv("SNIPER_THRESHOLD", "0.5"))  # buy if < 50% floor
        self.max_buy = float(os.getenv("SNIPER_MAX_BUY_ETH", "0.5"))
        self.rpc_url = os.getenv("SNIPER_RPC_URL", "https://eth-mainnet.g.alchemy.com/v2/demo")
        self.buyer_address = os.getenv("BUYER_WALLET_ADDRESS", "")
        self.buyer_key = os.getenv("BUYER_WALLET_PRIVATE_KEY", "")
        self.opensea_key = os.getenv("OPENSEA_API_KEY", "")

        # Collections whitelist (empty = snipe all)
        raw = os.getenv("SNIPER_COLLECTIONS", "")
        self.whitelist: set[str] = {a.strip().lower() for a in raw.split(",") if a.strip()}

        self.floor_cache = FloorCache()
        self.hits: list[SniperHit] = []
        self._running = False

        # Telegram
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

        if self.enabled:
            log.info("FatFingerSniper: threshold=%.0f%%, max_buy=%.3f ETH, "
                     "whitelist=%d collections, rpc=%s",
                     self.threshold * 100, self.max_buy,
                     len(self.whitelist), self.rpc_url[:40])

    # ── Floor price updates ─────────────────────────────────────

    def update_floor_from_sales(self, events: list) -> None:
        """Update floor cache from recent sales data (called by daemon).

        Uses minimum sale price in last batch as approximate floor.
        Not perfect but good enough for fat-finger detection.
        """
        from collections import defaultdict
        by_collection: dict[str, list[float]] = defaultdict(list)
        for e in events:
            if e.price > 0:
                by_collection[e.collection_address].append(e.price)

        for addr, prices in by_collection.items():
            # Use 10th percentile as approximate floor
            prices.sort()
            idx = max(0, len(prices) // 10)
            floor = prices[idx]
            self.floor_cache.set(addr, floor)

    async def update_floor_from_api(self, collection_address: str) -> float | None:
        """Fetch floor from OpenSea API."""
        if not self.opensea_key:
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.opensea.io/api/v2/collections/{collection_address}/stats"
                headers = {"X-API-KEY": self.opensea_key}
                async with session.get(url, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        floor = data.get("total", {}).get("floor_price", 0)
                        if floor and floor > 0:
                            self.floor_cache.set(collection_address, float(floor))
                            return float(floor)
        except Exception as exc:
            log.debug("Floor API fetch failed for %s: %s", collection_address[:10], exc)
        return None

    # ── Listing analysis ────────────────────────────────────────

    def check_listing(self, listing: dict) -> SniperHit | None:
        """Check if a listing is a potential fat finger.

        Called for each new listing from OpenSea Stream or OKX data.

        Args:
            listing: dict with keys:
                collection_address, token_id, price, currency,
                chain, order_hash, seller, collection_name
        """
        collection = listing.get("collection_address", "").lower()
        if not collection:
            return None

        # Whitelist check
        if self.whitelist and collection not in self.whitelist:
            return None

        price = float(listing.get("price", 0))
        if price <= 0:
            return None

        # Max spend check
        if price > self.max_buy:
            return None

        # Get floor
        floor = self.floor_cache.get(collection)
        if floor is None or floor <= 0:
            return None  # no floor data — can't determine if fat finger

        # Threshold check
        ratio = price / floor
        if ratio >= self.threshold:
            return None  # not cheap enough

        discount = 1.0 - ratio
        hit = SniperHit(
            collection_address=collection,
            collection_name=listing.get("collection_name", ""),
            token_id=str(listing.get("token_id", "")),
            listing_price=price,
            floor_price=floor,
            discount_pct=discount,
            currency=listing.get("currency", "ETH"),
            chain=listing.get("chain", "eth"),
            order_hash=listing.get("order_hash", ""),
            seller=listing.get("seller", ""),
            detected_at=datetime.now(timezone.utc).isoformat(),
        )

        log.warning(
            "🎯 FAT FINGER DETECTED: %s #%s listed @ %.6f %s "
            "(floor=%.4f, discount=%.0f%%)",
            hit.collection_name, hit.token_id, hit.listing_price,
            hit.currency, hit.floor_price, hit.discount_pct * 100,
        )

        self.hits.append(hit)
        return hit

    # ── Buy execution ───────────────────────────────────────────

    async def execute_buy(self, hit: SniperHit) -> SniperHit:
        """Buy the mispriced NFT via Seaport fulfillOrder.

        Phase 1: Just alert (no actual buy)
        Phase 2: Implement with web3.py
        """
        if not self.buyer_address or not self.buyer_key:
            hit.error = "BUYER_WALLET not configured"
            log.warning("Cannot buy: %s", hit.error)
            return hit

        # TODO Phase 2: Implement actual purchase
        # Steps:
        # 1. Fetch full order from OpenSea API by order_hash
        # 2. Build Seaport fulfillOrder calldata
        # 3. Sign tx with buyer private key via web3.py
        # 4. Submit to RPC (or Flashbots bundle for MEV protection)
        # 5. Wait for confirmation
        #
        # from web3 import Web3
        # w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        # seaport = w3.eth.contract(address=SEAPORT_ADDRESS, abi=SEAPORT_ABI)
        # tx = seaport.functions.fulfillOrder(order, conduitKey).build_transaction({
        #     'from': self.buyer_address,
        #     'value': int(hit.listing_price * 10**18),
        #     'gas': 300000,
        #     'maxPriorityFeePerGas': w3.to_wei(3, 'gwei'),  # aggressive tip
        # })
        # signed = w3.eth.account.sign_transaction(tx, self.buyer_key)
        # tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        hit.error = "AUTO_BUY_NOT_IMPLEMENTED_YET"
        log.info("Fat finger detected but auto-buy not yet implemented: %s #%s @ %.6f",
                 hit.collection_name, hit.token_id, hit.listing_price)
        return hit

    # ── Telegram alerts ─────────────────────────────────────────

    def alert_fat_finger(self, hit: SniperHit):
        """Send fat finger alert to Telegram."""
        if not self.tg_token or not self.tg_chat:
            return

        status = "✅ BOUGHT" if hit.bought else "⚠️ DETECTED (manual buy needed)"
        if hit.error:
            status = f"❌ {hit.error}"

        msg = (
            f"🎯 <b>FAT FINGER {status}</b>\n\n"
            f"Collection: <b>{hit.collection_name}</b>\n"
            f"Token: #{hit.token_id}\n"
            f"Listed: <b>{hit.listing_price:.6f} {hit.currency}</b>\n"
            f"Floor: {hit.floor_price:.4f} {hit.currency}\n"
            f"Discount: <b>{hit.discount_pct * 100:.0f}%</b>\n"
            f"Chain: {hit.chain.upper()}\n"
            f"Seller: <code>{hit.seller[:10]}...</code>\n"
            f"Time: {hit.detected_at}"
        )
        if hit.tx_hash:
            msg += f"\nTX: <code>{hit.tx_hash}</code>"

        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            try:
                from curl_cffi import requests as http
                http.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat, "text": msg, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as exc:
                log.warning("Telegram fat finger alert failed: %s", exc)

    # ── Integration with sales stream daemon ────────────────────

    def process_sales_batch(self, events: list) -> list[SniperHit]:
        """Called by daemon after each poll cycle.

        1. Updates floor cache from sales data
        2. Checks each sale for fat finger pattern
        3. Returns list of hits

        Note: This detects fat fingers from COMPLETED sales (after the fact).
        For realtime sniping, use OpenSea WebSocket stream (Phase 2).
        """
        if not self.enabled:
            return []

        # Update floors
        self.update_floor_from_sales(events)

        # Check each sale for anomaly (retroactive detection)
        hits = []
        for e in events:
            listing = {
                "collection_address": e.collection_address,
                "collection_name": e.collection_name,
                "token_id": e.token_id,
                "price": e.price,
                "currency": e.currency,
                "chain": e.chain,
                "order_hash": e.tx_hash,
                "seller": e.seller,
            }
            hit = self.check_listing(listing)
            if hit:
                hits.append(hit)
                self.alert_fat_finger(hit)

        return hits
