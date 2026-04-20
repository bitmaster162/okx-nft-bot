"""
Offer Blaster — auto mass-offer on Binance WL collections.

When a Binance WL trade is detected:
    1. Fetch all NFTs in that collection from OKX
    2. Fetch existing offers to find undercut targets
    3. Place offers at min(existing_lowest - delta, max_offer_price) up to N items
    4. Skip items we already have offers on

Reuses existing MassOfferEngine infrastructure for BSC.
For ETH: uses multi-chain seaport signing.

Env vars:
    OFFER_BLASTER_ENABLED=0
    OFFER_BLASTER_DRY_RUN=1
    OFFER_BLASTER_MAX_PER_COLLECTION=50
    OFFER_BLASTER_DELAY_SECONDS=1.0
    OFFER_BLASTER_DURATION_HOURS=24
    OFFER_BLASTER_UNDERCUT_BPS=100    # undercut by 1% (100 basis points)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from okx_nft_bot.undercutter.state import PositionState
from okx_nft_bot.wei_utils import to_wei

log = logging.getLogger("sniper.offer_blaster")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


@dataclass
class BlasterResult:
    collection_address: str
    collection_name: str
    chain: str
    offers_placed: int
    offers_failed: int
    offers_skipped: int
    price_used: float
    currency: str
    dry_run: bool
    latency_ms: int = 0
    error: str | None = None


class OfferBlaster:
    """Auto mass-offer on Binance WL collections when triggered by sales stream."""

    def __init__(self):
        self.enabled = os.getenv("OFFER_BLASTER_ENABLED", "0").lower() in ("1", "true", "yes")
        self.dry_run = os.getenv("OFFER_BLASTER_DRY_RUN", "1").lower() in ("1", "true", "yes")
        self.execution_db_path = Path(os.getenv("EXECUTION_DB_PATH", "./data/execution.sqlite3"))
        self.max_per_collection = int(os.getenv("OFFER_BLASTER_MAX_PER_COLLECTION", "50"))
        self.delay_seconds = float(os.getenv("OFFER_BLASTER_DELAY_SECONDS", "1.0"))
        self.duration_hours = int(os.getenv("OFFER_BLASTER_DURATION_HOURS", "24"))
        self.undercut_bps = int(os.getenv("OFFER_BLASTER_UNDERCUT_BPS", "100"))  # 1%

        self.buyer_address = os.getenv("BUYER_WALLET_ADDRESS", "")
        self.buyer_key = os.getenv("BUYER_WALLET_PRIVATE_KEY", "")

        # OKX API
        self.okx_api_key = os.getenv("OKX_API_KEY", "")
        self.okx_api_secret = os.getenv("OKX_API_SECRET", "")
        self.okx_api_passphrase = os.getenv("OKX_API_PASSPHRASE", "")

        # Telegram
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

        # Track recent blasts to avoid spamming same collection
        self._recent_blasts: dict[str, float] = {}  # collection_addr -> last_blast_time
        self._cooldown_seconds = 300  # 5 min cooldown per collection
        self._lock = threading.Lock()

        # Lazy-init heavy deps
        self._settings = None
        self._engine = None

        if self.enabled:
            log.info("OfferBlaster: ENABLED, effective_dry_run=%s, max=%d/collection, "
                     "delay=%.1fs, duration=%dh, undercut=%dbps",
                     self._effective_dry_run(), self.max_per_collection,
                     self.delay_seconds, self.duration_hours, self.undercut_bps)
        else:
            log.info("OfferBlaster: DISABLED (set OFFER_BLASTER_ENABLED=1)")

    def _effective_dry_run(self) -> bool:
        global_dry_run = _env_bool("DRY_RUN", True)
        try:
            state = PositionState(self.execution_db_path)
            state.audit_integrity()
            forced_dry_run = state.is_force_dry_run()
        except Exception as exc:
            log.warning(
                "OfferBlaster: failed to read execution runtime state from %s: %s; forcing dry-run",
                self.execution_db_path,
                exc,
            )
            return True
        return bool(self.dry_run or global_dry_run or forced_dry_run)

    def _get_settings(self):
        """Lazy-load Settings."""
        if self._settings is None:
            try:
                from okx_nft_bot.config import load_settings
                self._settings = load_settings()
            except Exception as exc:
                log.error("Failed to load settings for OfferBlaster: %s", exc)
        return self._settings

    def _get_engine(self):
        """Lazy-load MassOfferEngine."""
        if self._engine is None:
            try:
                settings = self._get_settings()
                if settings:
                    from okx_nft_bot.mass_offer.engine import MassOfferEngine
                    self._engine = MassOfferEngine(settings=settings)
            except Exception as exc:
                log.error("Failed to init MassOfferEngine: %s", exc)
        return self._engine

    def should_blast(self, collection_address: str) -> bool:
        """Check if we should blast this collection (cooldown check)."""
        if not self.enabled:
            return False
        addr = collection_address.lower()
        with self._lock:
            last = self._recent_blasts.get(addr, 0)
            if time.time() - last < self._cooldown_seconds:
                return False
        return True

    def blast_collection(
        self,
        *,
        collection_address: str,
        collection_name: str,
        chain: str,
        max_offer_price: float,
        currency: str = "ETH",
    ) -> BlasterResult | None:
        """Fire mass offers on a collection.

        1. For BSC: use MassOfferEngine directly (Seaport + WBNB)
        2. For ETH: use OKX API with Seaport signing (ETH-adapted)
        """
        if not self.enabled:
            return None

        addr = collection_address.lower()
        chain_lower = chain.lower()
        if chain_lower == "ethereum":
            chain_lower = "eth"

        # Cooldown check
        with self._lock:
            last = self._recent_blasts.get(addr, 0)
            if time.time() - last < self._cooldown_seconds:
                log.debug("OfferBlaster: %s on cooldown, skip", collection_name)
                return None
            self._recent_blasts[addr] = time.time()

        effective_dry_run = self._effective_dry_run()
        t0 = time.monotonic()
        log.info("🔫 OFFER BLAST: %s [%s] max_price=%.6f %s, max=%d",
                 collection_name, chain_lower, max_offer_price, currency,
                 self.max_per_collection)

        result = BlasterResult(
            collection_address=addr,
            collection_name=collection_name,
            chain=chain_lower,
            offers_placed=0,
            offers_failed=0,
            offers_skipped=0,
            price_used=max_offer_price,
            currency=currency,
            dry_run=effective_dry_run,
        )

        try:
            if chain_lower == "bsc":
                self._blast_bsc(result, addr, max_offer_price, effective_dry_run)
            elif chain_lower == "eth":
                self._blast_eth(result, addr, max_offer_price, effective_dry_run)
            else:
                result.error = f"Unsupported chain: {chain_lower}"
                log.warning("OfferBlaster: %s", result.error)
        except Exception as exc:
            result.error = str(exc)
            log.error("OfferBlaster failed for %s: %s", collection_name, exc)

        result.latency_ms = int((time.monotonic() - t0) * 1000)
        self._alert_result(result)

        log.info("🔫 BLAST DONE: %s — placed=%d, failed=%d, skipped=%d, %dms",
                 collection_name, result.offers_placed, result.offers_failed,
                 result.offers_skipped, result.latency_ms)
        return result

    # ── BSC path: MassOfferEngine ────────────────────────────────

    def _blast_bsc(self, result: BlasterResult, collection: str, max_price_bnb: float, effective_dry_run: bool):
        """Use existing MassOfferEngine for BSC (Seaport + WBNB)."""
        engine = self._get_engine()
        if engine is None:
            result.error = "MassOfferEngine not available"
            return

        try:
            run_result = engine.run(
                collection=collection,
                chain="bsc",
                price_bnb=max_price_bnb,
                max_total=self.max_per_collection,
                duration_hours=self.duration_hours,
                delay_seconds=self.delay_seconds,
                dry_run=effective_dry_run,
                exclude_own=True,
                max_existing_offer=max_price_bnb,  # skip if existing offer >= our price
            )
            result.offers_placed = run_result.submitted_count
            result.offers_failed = run_result.failed_count
            result.offers_skipped = run_result.skipped_count
        except Exception as exc:
            result.error = str(exc)
            log.error("BSC blast error: %s", exc)

    # ── ETH path: direct Seaport signing ─────────────────────────

    def _blast_eth(self, result: BlasterResult, collection: str, max_price_eth: float, effective_dry_run: bool):
        """For ETH: fetch NFTs, build offers, sign with Seaport, submit to OKX.

        The existing MassOfferEngine only supports BSC, so we replicate the
        core flow with ETH-specific constants.
        """
        settings = self._get_settings()
        if not settings or not settings.buyer_wallet_address or not settings.buyer_wallet_private_key:
            result.error = "BUYER_WALLET not configured"
            return

        try:
            from eth_account import Account
            from okx_nft_bot.clients.okx import OKXMarketplaceClient
            from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
            import base64, hashlib, hmac
            from datetime import datetime, timezone

            account = Account.from_key(settings.buyer_wallet_private_key)
            offerer = account.address

            # ETH Seaport constants
            ETH_CHAIN_ID = 1
            SEAPORT_ADDR = "0x0000000000000068F116a894984e2DB1123eB395"
            WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
            ETH_RPC = os.getenv("BUYER_RPC_URL_ETH", "https://eth.llamarpc.com")
            CONDUIT_KEY = "0x618Cf13c76c1FFC2168fC47c98453dCc6134F5c8888888888888888888888888"

            # 1. Get counter from Seaport
            transport = StdlibHttpTransport(timeout=10, max_retries=2, rate_limit_per_sec=5.0)
            counter = self._get_seaport_counter(offerer, SEAPORT_ADDR, ETH_RPC, transport)

            # 2. Fetch NFTs in collection
            market_client = OKXMarketplaceClient(settings=settings)
            nfts = self._fetch_collection_nfts_paginated(market_client, collection, "eth", max_pages=5)
            if not nfts:
                result.error = "No NFTs found in collection"
                return

            log.info("Found %d NFTs in %s [ETH], will offer on up to %d",
                     len(nfts), collection[:10], self.max_per_collection)

            # 3. Fetch existing offers to undercut
            existing_offers = self._fetch_existing_offers(market_client, collection, "eth")

            # 4. Build and submit offers
            price_wei = to_wei(max_price_eth)
            duration_s = self.duration_hours * 3600
            placed = 0
            failed = 0
            skipped = 0

            for nft in nfts[:self.max_per_collection * 2]:  # fetch extra for skips
                if placed >= self.max_per_collection:
                    break

                token_id = nft.get("tokenId") or nft.get("token_id")
                if token_id is None:
                    skipped += 1
                    continue
                token_id = int(token_id)

                # Skip if we're the owner
                owner = (nft.get("ownerAddress") or nft.get("owner") or "").lower()
                if owner == offerer.lower():
                    skipped += 1
                    continue

                # Determine offer price — undercut existing if possible
                existing_best = existing_offers.get(str(token_id), 0)
                if existing_best > 0:
                    # Undercut by bps
                    undercut_price = existing_best * (1 - self.undercut_bps / 10000)
                    offer_price_eth = min(max_price_eth, undercut_price)
                    offer_price_wei = to_wei(offer_price_eth)
                else:
                    offer_price_eth = max_price_eth
                    offer_price_wei = price_wei

                if offer_price_wei <= 0:
                    skipped += 1
                    continue

                try:
                    # Build Seaport offer order (WETH → ERC721)
                    parameters = self._build_eth_offer(
                        offerer=offerer,
                        collection=collection,
                        token_id=token_id,
                        price_wei=offer_price_wei,
                        counter=counter,
                        duration_s=duration_s,
                        weth_address=WETH_ADDRESS,
                        conduit_key=CONDUIT_KEY,
                    )

                    # Sign
                    signature = self._sign_eth_order(parameters, settings.buyer_wallet_private_key,
                                                     ETH_CHAIN_ID, SEAPORT_ADDR)

                    payload = {
                        "chain": "eth",
                        "offerer": offerer,
                        "collection": collection.lower(),
                        "token_id": token_id,
                        "price_eth": offer_price_eth,
                        "price_wei": offer_price_wei,
                        "counter": counter,
                        "parameters": parameters,
                        "signature": signature,
                    }

                    if effective_dry_run:
                        log.debug("DRY RUN offer: %s #%d @ %.6f ETH", collection[:10], token_id, offer_price_eth)
                        placed += 1
                    else:
                        # Submit to OKX
                        from okx_nft_bot.counterbid.okx_api import OKXAPIClient
                        api_client = OKXAPIClient(settings=settings, market_client=market_client)
                        submit_result = api_client.submit_offer(payload)
                        offer_id = submit_result.get("offer_id", "?")
                        log.info("Offer submitted: %s #%d @ %.6f ETH → %s",
                                 collection[:10], token_id, offer_price_eth, offer_id)
                        placed += 1

                    # ── Increment counter for next offer (Seaport requires unique counter per order)
                    counter += 1

                    # Delay between submissions
                    if self.delay_seconds > 0:
                        time.sleep(self.delay_seconds)

                except Exception as exc:
                    log.warning("Offer failed for #%d: %s", token_id, exc)
                    failed += 1

            result.offers_placed = placed
            result.offers_failed = failed
            result.offers_skipped = skipped

        except ImportError as exc:
            result.error = f"Missing dependency: {exc}"
            log.error("ETH blast import error: %s", exc)
        except Exception as exc:
            result.error = str(exc)
            log.error("ETH blast error: %s", exc, exc_info=True)

    # ── Helpers ───────────────────────────────────────────────────

    def _get_seaport_counter(self, offerer: str, seaport_addr: str, rpc_url: str,
                              transport) -> int:
        """Get Seaport counter for offerer address."""
        GET_COUNTER_SELECTOR = "0xf07ec373"
        addr_clean = offerer.lower().replace("0x", "")
        calldata = GET_COUNTER_SELECTOR + addr_clean.zfill(64)
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": seaport_addr, "data": calldata}, "latest"],
            "id": 1,
        }
        import json as _json
        result = transport.request_json(
            method="POST", url=rpc_url,
            headers={"Content-Type": "application/json"},
            body=_json.dumps(payload),
        )
        if "error" in result:
            raise RuntimeError(f"RPC error getting counter: {result['error']}")
        return int(str(result["result"]), 16)

    def _fetch_collection_nfts_paginated(self, client, collection: str, chain: str,
                                          max_pages: int = 5) -> list[dict]:
        """Fetch NFTs in collection from OKX, paginated."""
        all_items = []
        cursor = None
        for _ in range(max_pages):
            try:
                resp = client.get_nft_list(
                    chain=chain,
                    contract_address=collection,
                    limit=20,
                    cursor=cursor,
                )
                items = resp.get("data", [])
                if isinstance(items, dict):
                    cursor = items.get("cursor")
                    items = items.get("data", [])
                if not items:
                    break
                all_items.extend(items)
                if not cursor:
                    break
            except Exception as exc:
                log.warning("NFT fetch page failed: %s", exc)
                break
        return all_items

    def _fetch_existing_offers(self, client, collection: str, chain: str) -> dict[str, float]:
        """Fetch highest existing offers per token_id. Returns {token_id_str: price_float}."""
        offer_map: dict[str, float] = {}
        try:
            resp = client.get_offers(
                chain=chain,
                collection_address=collection,
                status="active",
                limit=100,
            )
            items = resp.get("data", [])
            if isinstance(items, dict):
                items = items.get("data", [])
            for item in items:
                tid = str(item.get("tokenId") or item.get("token_id") or "")
                if not tid:
                    continue
                try:
                    price = float(item.get("price") or item.get("offerPrice") or 0)
                except (TypeError, ValueError):
                    price = 0
                if price > offer_map.get(tid, 0):
                    offer_map[tid] = price
        except Exception as exc:
            log.debug("Fetch existing offers failed: %s", exc)
        return offer_map

    def _build_eth_offer(self, *, offerer: str, collection: str, token_id: int,
                          price_wei: int, counter: int, duration_s: int,
                          weth_address: str, conduit_key: str) -> dict:
        """Build Seaport offer order for ETH chain (WETH → ERC721)."""
        import hashlib as _hl
        import time as _time

        now = int(_time.time())
        raw = f"{offerer}:{collection}:{token_id}:{price_wei}:{now}:token:{token_id}".encode()
        salt = int(_hl.sha256(raw).hexdigest(), 16) % (2**256)

        return {
            "offerer": offerer,
            "zone": "0x0000000000000000000000000000000000000000",
            "offer": [
                {
                    "itemType": 1,  # ERC20 (WETH)
                    "token": weth_address,
                    "identifierOrCriteria": 0,
                    "startAmount": str(price_wei),
                    "endAmount": str(price_wei),
                }
            ],
            "consideration": [
                {
                    "itemType": 2,  # ERC721
                    "token": collection,
                    "identifierOrCriteria": token_id,
                    "startAmount": "1",
                    "endAmount": "1",
                    "recipient": offerer,
                }
            ],
            "orderType": 0,  # FULL_OPEN
            "startTime": str(now),
            "endTime": str(now + duration_s),
            "zoneHash": "0x" + ("00" * 32),
            "salt": str(salt),
            "conduitKey": conduit_key,
            "counter": str(counter),
            "totalOriginalConsiderationItems": 1,
        }

    def _sign_eth_order(self, payload: dict, private_key: str,
                         chain_id: int, seaport_address: str) -> str:
        """Sign Seaport order with EIP-712 for ETH chain."""
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        EIP712_TYPES = {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "OrderComponents": [
                {"name": "offerer", "type": "address"},
                {"name": "zone", "type": "address"},
                {"name": "offer", "type": "OfferItem[]"},
                {"name": "consideration", "type": "ConsiderationItem[]"},
                {"name": "orderType", "type": "uint8"},
                {"name": "startTime", "type": "uint256"},
                {"name": "endTime", "type": "uint256"},
                {"name": "zoneHash", "type": "bytes32"},
                {"name": "salt", "type": "uint256"},
                {"name": "conduitKey", "type": "bytes32"},
                {"name": "counter", "type": "uint256"},
            ],
            "OfferItem": [
                {"name": "itemType", "type": "uint8"},
                {"name": "token", "type": "address"},
                {"name": "identifierOrCriteria", "type": "uint256"},
                {"name": "startAmount", "type": "uint256"},
                {"name": "endAmount", "type": "uint256"},
            ],
            "ConsiderationItem": [
                {"name": "itemType", "type": "uint8"},
                {"name": "token", "type": "address"},
                {"name": "identifierOrCriteria", "type": "uint256"},
                {"name": "startAmount", "type": "uint256"},
                {"name": "endAmount", "type": "uint256"},
                {"name": "recipient", "type": "address"},
            ],
        }

        domain = {
            "name": "Seaport",
            "version": "1.6",
            "chainId": chain_id,
            "verifyingContract": seaport_address,
        }

        def _offer_item(item):
            return {
                "itemType": int(item["itemType"]),
                "token": item["token"],
                "identifierOrCriteria": int(item["identifierOrCriteria"]),
                "startAmount": int(item["startAmount"]),
                "endAmount": int(item["endAmount"]),
            }

        def _consideration_item(item):
            return {
                "itemType": int(item["itemType"]),
                "token": item["token"],
                "identifierOrCriteria": int(item["identifierOrCriteria"]),
                "startAmount": int(item["startAmount"]),
                "endAmount": int(item["endAmount"]),
                "recipient": item["recipient"],
            }

        message = {
            "offerer": payload["offerer"],
            "zone": payload["zone"],
            "offer": [_offer_item(i) for i in payload["offer"]],
            "consideration": [_consideration_item(i) for i in payload["consideration"]],
            "orderType": int(payload["orderType"]),
            "startTime": int(payload["startTime"]),
            "endTime": int(payload["endTime"]),
            "zoneHash": bytes.fromhex(str(payload["zoneHash"]).replace("0x", "")),
            "salt": int(payload["salt"]),
            "conduitKey": bytes.fromhex(str(payload["conduitKey"]).replace("0x", "")),
            "counter": int(payload["counter"]),
        }

        structured = {
            "types": EIP712_TYPES,
            "domain": domain,
            "primaryType": "OrderComponents",
            "message": message,
        }

        encoded = encode_typed_data(full_message=structured)
        signed = Account.sign_message(encoded, private_key=private_key)
        from okx_nft_bot.signing.seaport_signer import hex_with_prefix
        return hex_with_prefix(signed.signature)

    # ── Telegram alert ────────────────────────────────────────────

    def _alert_result(self, result: BlasterResult):
        """Send blast result to Telegram."""
        if not self.tg_token or not self.tg_chat:
            return

        if result.dry_run:
            emoji = "🏷️"
            mode = "DRY RUN"
        elif result.offers_placed > 0:
            emoji = "🔫"
            mode = "LIVE"
        else:
            emoji = "⚠️"
            mode = "NO OFFERS"

        error_line = f"\nError: {result.error}" if result.error else ""
        msg = (
            f"{emoji} <b>Offer Blast [{mode}]</b>\n"
            f"Collection: {result.collection_name}\n"
            f"Chain: {result.chain.upper()}\n"
            f"Price: {result.price_used:.6f} {result.currency}\n"
            f"Placed: {result.offers_placed}\n"
            f"Failed: {result.offers_failed}\n"
            f"Skipped: {result.offers_skipped}\n"
            f"Time: {result.latency_ms}ms"
            f"{error_line}"
        )

        try:
            from okx_nft_bot.sales_stream import http
            http.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            try:
                import urllib.request
                import json as _json
                data = _json.dumps({
                    "chat_id": self.tg_chat, "text": msg, "parse_mode": "HTML",
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    data=data, headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                log.warning("Telegram notify failed: %s", exc)
