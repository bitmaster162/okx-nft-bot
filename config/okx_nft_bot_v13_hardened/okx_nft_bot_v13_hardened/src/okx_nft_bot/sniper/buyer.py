"""
OKX Instant Buyer — buy NFTs on OKX marketplace ASAP via Seaport fulfillOrder.

Flow (optimised for speed):
    1. Fetch cheapest listing for a collection from OKX API
    2. Parse Seaport order parameters from listing response
    3. Build fulfillBasicOrder / fulfillOrder calldata
    4. Sign tx with buyer private key
    5. Submit raw tx to fast RPC with aggressive gas

Env vars:
    BUYER_WALLET_ADDRESS=0x...
    BUYER_WALLET_PRIVATE_KEY=0x...
    BUYER_RPC_URL=https://bsc-dataseed.binance.org/  (or paid Alchemy/QuickNode)
    BUYER_RPC_URL_ETH=https://eth-mainnet.g.alchemy.com/v2/...
    AUTO_BUY_ENABLED=0         # set 1 to enable real purchases
    AUTO_BUY_DRY_RUN=1         # 1 = log only, 0 = execute on-chain
    AUTO_BUY_GAS_MULTIPLIER=1.2
    AUTO_BUY_MAX_GAS_GWEI=50   # max gas price, abort if higher
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from okx_nft_bot.undercutter.state import PositionState
from okx_nft_bot.wei_utils import to_wei, to_gwei

log = logging.getLogger("sniper.buyer")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")

# Seaport v1.6 — matches OKX marketplace (same address for BSC + ETH)
SEAPORT_ADDRESS = "0x0000000000000068F116a894984e2DB1123eB395"

# Minimal ABI for fulfillBasicOrder + fulfillOrder
SEAPORT_ABI_MINIMAL = json.loads("""[
  {
    "inputs": [
      {
        "components": [
          {"internalType":"address","name":"considerationToken","type":"address"},
          {"internalType":"uint256","name":"considerationIdentifier","type":"uint256"},
          {"internalType":"uint256","name":"considerationAmount","type":"uint256"},
          {"internalType":"address payable","name":"offerer","type":"address"},
          {"internalType":"address","name":"zone","type":"address"},
          {"internalType":"address","name":"offerToken","type":"address"},
          {"internalType":"uint256","name":"offerIdentifier","type":"uint256"},
          {"internalType":"uint256","name":"offerAmount","type":"uint256"},
          {"internalType":"uint8","name":"basicOrderType","type":"uint8"},
          {"internalType":"uint256","name":"startTime","type":"uint256"},
          {"internalType":"uint256","name":"endTime","type":"uint256"},
          {"internalType":"bytes32","name":"zoneHash","type":"bytes32"},
          {"internalType":"uint256","name":"salt","type":"uint256"},
          {"internalType":"bytes32","name":"offererConduitKey","type":"bytes32"},
          {"internalType":"bytes32","name":"fulfillerConduitKey","type":"bytes32"},
          {"internalType":"uint256","name":"totalOriginalAdditionalRecipients","type":"uint256"},
          {
            "components": [
              {"internalType":"uint256","name":"amount","type":"uint256"},
              {"internalType":"address payable","name":"recipient","type":"address"}
            ],
            "internalType":"struct AdditionalRecipient[]",
            "name":"additionalRecipients",
            "type":"tuple[]"
          },
          {"internalType":"bytes","name":"signature","type":"bytes"}
        ],
        "internalType":"struct BasicOrderParameters",
        "name":"parameters",
        "type":"tuple"
      }
    ],
    "name":"fulfillBasicOrder",
    "outputs":[{"internalType":"bool","name":"fulfilled","type":"bool"}],
    "stateMutability":"payable",
    "type":"function"
  }
]""")

# Chain configs
CHAIN_CONFIG = {
    "eth": {
        "chain_id": 1,
        "rpc_env": "BUYER_RPC_URL_ETH",
        "rpc_default": "https://eth.llamarpc.com",
        "native": "ETH",
        "explorer": "https://etherscan.io/tx/",
        "okx_chain": "eth",
    },
    "ethereum": {
        "chain_id": 1,
        "rpc_env": "BUYER_RPC_URL_ETH",
        "rpc_default": "https://eth.llamarpc.com",
        "native": "ETH",
        "explorer": "https://etherscan.io/tx/",
        "okx_chain": "eth",
    },
    "bsc": {
        "chain_id": 56,
        "rpc_env": "BUYER_RPC_URL",
        "rpc_default": "https://bsc-dataseed.binance.org/",
        "native": "BNB",
        "explorer": "https://bscscan.com/tx/",
        "okx_chain": "bsc",
    },
}


@dataclass
class BuyAttempt:
    collection_address: str
    collection_name: str
    token_id: str
    chain: str
    listing_price: float
    currency: str
    max_buy_price: float
    success: bool = False
    tx_hash: str | None = None
    error: str | None = None
    gas_used: int = 0
    latency_ms: int = 0
    dry_run: bool = True


class OKXInstantBuyer:
    """Speed-optimised buyer for OKX NFT marketplace.

    Usage:
        buyer = OKXInstantBuyer()
        result = buyer.try_buy(
            collection_address="0x...",
            collection_name="CoolCats",
            chain="eth",
            max_price=0.00025,
        )
    """

    def __init__(self):
        self.enabled = os.getenv("AUTO_BUY_ENABLED", "0").lower() in ("1", "true", "yes")
        self.dry_run = os.getenv("AUTO_BUY_DRY_RUN", "1").lower() in ("1", "true", "yes")
        self.execution_db_path = Path(os.getenv("EXECUTION_DB_PATH", "./data/execution.sqlite3"))
        self.buyer_address = os.getenv("BUYER_WALLET_ADDRESS", "")
        self.buyer_key = os.getenv("BUYER_WALLET_PRIVATE_KEY", "")
        self.gas_multiplier = float(os.getenv("AUTO_BUY_GAS_MULTIPLIER", "1.2"))
        self.max_gas_gwei = float(os.getenv("AUTO_BUY_MAX_GAS_GWEI", "50"))

        # OKX API creds (for fetching listings)
        self.okx_api_key = os.getenv("OKX_API_KEY", "")
        self.okx_api_secret = os.getenv("OKX_API_SECRET", "")
        self.okx_api_passphrase = os.getenv("OKX_API_PASSPHRASE", "")
        self.okx_api_base = os.getenv("OKX_API_BASE", "https://web3.okx.com")

        # Pre-init web3 connections (keep alive for speed)
        self._w3: dict[str, Any] = {}  # chain -> Web3 instance
        self._nonce_cache: dict[str, int] = {}  # chain -> last nonce
        self._lock = threading.Lock()

        # Stats
        self.total_attempts = 0
        self.total_buys = 0
        self.total_spent: dict[str, float] = {}  # chain -> total native spent

        # Telegram
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

        if self.enabled:
            self._init_web3()
            log.info("OKXInstantBuyer: ENABLED, effective_dry_run=%s, wallet=%s, gas_mult=%.1f, max_gas=%d gwei",
                     self._effective_dry_run(), self.buyer_address[:10] + "..." if self.buyer_address else "NONE",
                     self.gas_multiplier, self.max_gas_gwei)
        else:
            log.info("OKXInstantBuyer: DISABLED (set AUTO_BUY_ENABLED=1 to enable)")

    def _effective_dry_run(self) -> bool:
        global_dry_run = _env_bool("DRY_RUN", True)
        try:
            state = PositionState(self.execution_db_path)
            state.audit_integrity()
            forced_dry_run = state.is_force_dry_run()
        except Exception as exc:
            log.warning(
                "OKXInstantBuyer: failed to read execution runtime state from %s: %s; forcing dry-run",
                self.execution_db_path,
                exc,
            )
            return True
        return bool(self.dry_run or global_dry_run or forced_dry_run)

    def _init_web3(self):
        """Pre-initialise Web3 instances for all chains."""
        try:
            from web3 import Web3
            for chain_name, cfg in CHAIN_CONFIG.items():
                if chain_name in ("ethereum",):  # skip alias
                    continue
                rpc_url = os.getenv(cfg["rpc_env"], cfg["rpc_default"])
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                if w3.is_connected():
                    self._w3[chain_name] = w3
                    log.info("Web3 connected: %s → %s (chainId=%s)",
                             chain_name, rpc_url[:40], w3.eth.chain_id)
                else:
                    log.warning("Web3 NOT connected: %s → %s", chain_name, rpc_url[:40])
        except ImportError:
            log.error("web3 not installed! pip install web3")
        except Exception as exc:
            log.error("Web3 init failed: %s", exc)

    def _get_w3(self, chain: str):
        """Get Web3 instance for chain."""
        chain = chain.lower()
        if chain == "ethereum":
            chain = "eth"
        return self._w3.get(chain)

    # ── Listing fetch (OKX v5 API) ────────────────────────────────

    def _fetch_listings(self, collection_address: str, chain: str, limit: int = 20) -> list[dict]:
        """Fetch current listings from OKX API, sorted by price ascending."""
        try:
            from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
            import base64, hashlib, hmac
            from datetime import datetime, timezone

            okx_chain = CHAIN_CONFIG.get(chain.lower(), {}).get("okx_chain", chain)
            transport = StdlibHttpTransport(timeout=10, max_retries=2, rate_limit_per_sec=5.0)

            path = "/api/v5/mktplace/nft/markets/listings"
            params = {
                "chain": okx_chain,
                "collectionAddress": collection_address,
                "limit": limit,
            }

            url, request_path = build_url(self.okx_api_base, path, params)
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            body = ""
            message = f"{timestamp}GET{request_path}{body}"
            digest = hmac.new(self.okx_api_secret.encode(), message.encode(), hashlib.sha256).digest()
            signature = base64.b64encode(digest).decode()

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "OK-ACCESS-KEY": self.okx_api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.okx_api_passphrase,
            }

            resp = transport.request_json(method="GET", url=url, headers=headers, body=body)
            items = resp.get("data", [])
            if isinstance(items, dict):
                items = items.get("data", [])
            if not isinstance(items, list):
                return []

            # Normalise prices from raw (wei) to human-readable.
            # OKX listing API returns prices as raw integers (e.g. 43911289990000000
            # for 0.044 ETH).  We need human-readable values so try_buy() can
            # compare them directly against max_buy_price from buy_config.
            for item in items:
                raw = item.get("price") or item.get("listingPrice") or "0"
                try:
                    raw_f = float(raw)
                except (TypeError, ValueError):
                    raw_f = 0.0
                # Heuristic: if value > 1e12 it's certainly in wei (18 decimals).
                # Human-readable prices are never above a few thousand.
                if raw_f > 1e12:
                    item["_price_human"] = raw_f / 1e18
                else:
                    item["_price_human"] = raw_f

            # Sort by normalised price ascending for fastest cheapest-first
            def _price(item):
                return item.get("_price_human", 999999.0)
            items.sort(key=_price)
            return items

        except Exception as exc:
            log.error("Fetch listings failed for %s [%s]: %s", collection_address[:10], chain, exc)
            return []

    # ── Core buy logic ────────────────────────────────────────────

    def try_buy(self, *, collection_address: str, collection_name: str,
                chain: str, max_price: float, currency: str = "ETH") -> BuyAttempt | None:
        """Try to buy the cheapest listing in a collection if price ≤ max_price.

        Returns BuyAttempt with result, or None if nothing to buy / disabled.
        """
        if not self.enabled:
            return None

        t0 = time.monotonic()
        self.total_attempts += 1
        chain_lower = chain.lower()
        if chain_lower == "ethereum":
            chain_lower = "eth"

        # 1. Fetch listings
        listings = self._fetch_listings(collection_address, chain_lower)
        if not listings:
            log.debug("No listings found for %s [%s]", collection_name, chain_lower)
            return None

        # 2. Find cheapest ≤ max_price (uses normalised _price_human)
        cheapest = None
        cheapest_price = 999999.0
        for item in listings:
            p = item.get("_price_human", 999999.0)
            if p <= max_price and p < cheapest_price:
                cheapest = item
                cheapest_price = p

        if cheapest is None:
            cheapest_shown = listings[0].get("_price_human", 0) if listings else 0
            log.info("💎 %s [%s]: cheapest listing %.6f > max %.6f — skip",
                     collection_name, chain_lower, cheapest_shown, max_price)
            return None

        token_id = str(cheapest.get("tokenId") or cheapest.get("token_id") or "?")
        log.warning("🔥 BUY TARGET: %s #%s @ %.6f %s (max=%.6f) [%s]",
                    collection_name, token_id, cheapest_price, currency, max_price, chain_lower)

        effective_dry_run = self._effective_dry_run()
        attempt = BuyAttempt(
            collection_address=collection_address,
            collection_name=collection_name,
            token_id=token_id,
            chain=chain_lower,
            listing_price=cheapest_price,
            currency=currency,
            max_buy_price=max_price,
            dry_run=effective_dry_run,
        )

        # 3. Execute buy
        if effective_dry_run:
            attempt.success = False
            attempt.error = "DRY_RUN"
            attempt.latency_ms = int((time.monotonic() - t0) * 1000)
            log.info("🏷️ DRY RUN: would buy %s #%s @ %.6f %s",
                     collection_name, token_id, cheapest_price, currency)
            self._alert_buy_attempt(attempt)
            return attempt

        # LIVE buy
        try:
            result = self._execute_buy(cheapest, chain_lower, cheapest_price)
            attempt.success = result.get("success", False)
            attempt.tx_hash = result.get("tx_hash")
            attempt.error = result.get("error")
            attempt.gas_used = result.get("gas_used", 0)
            if attempt.success:
                self.total_buys += 1
                self.total_spent[chain_lower] = self.total_spent.get(chain_lower, 0) + cheapest_price
        except Exception as exc:
            attempt.success = False
            attempt.error = str(exc)
            log.error("Buy execution failed: %s", exc)

        attempt.latency_ms = int((time.monotonic() - t0) * 1000)
        self._alert_buy_attempt(attempt)
        return attempt

    def _listing_price(self, listing: dict) -> float:
        """Return human-readable price (already normalised by _fetch_listings)."""
        return listing.get("_price_human", 999999.0)

    def _execute_buy(self, listing: dict, chain: str, price: float) -> dict[str, Any]:
        """Execute on-chain Seaport fulfillBasicOrder.

        Listing dict from OKX API contains order parameters.
        We build fulfillBasicOrder calldata, sign the tx, and submit.
        """
        w3 = self._get_w3(chain)
        if w3 is None:
            return {"success": False, "error": f"Web3 not connected for chain {chain}"}

        if not self.buyer_address or not self.buyer_key:
            return {"success": False, "error": "BUYER_WALLET not configured"}

        try:
            from web3 import Web3
            from eth_account import Account

            chain_cfg = CHAIN_CONFIG.get(chain, CHAIN_CONFIG["eth"])

            # Parse order from listing
            order_data = listing.get("orderData") or listing.get("order") or listing
            order_id = listing.get("orderId") or listing.get("orderHash") or ""

            # Get order details from OKX if needed
            if not order_data.get("parameters"):
                order_data = self._fetch_order_details(order_id, chain) or order_data

            params = order_data.get("parameters", order_data)

            # Build fulfillBasicOrder parameters
            # This handles the common case of ETH/BNB → ERC721 (BasicOrderType 0)
            seaport = w3.eth.contract(
                address=Web3.to_checksum_address(SEAPORT_ADDRESS),
                abi=SEAPORT_ABI_MINIMAL,
            )

            # Native token price in wei
            price_wei = to_wei(price)

            # Parse consideration items for additional recipients (royalties, fees)
            consideration = params.get("consideration", [])
            additional_recipients = []
            total_consideration = price_wei

            # First consideration item is the seller's payment
            # Remaining are additional recipients (platform fee, royalties)
            for i, c in enumerate(consideration):
                if i == 0:
                    continue  # seller payment
                amount = int(c.get("startAmount", 0))
                recipient = c.get("recipient", "")
                if amount > 0 and recipient:
                    additional_recipients.append((amount, recipient))

            # Offer items (the NFT)
            offer = params.get("offer", [{}])
            offer_token = offer[0].get("token", "") if offer else ""
            offer_id = int(offer[0].get("identifierOrCriteria", 0)) if offer else 0

            basic_params = {
                "considerationToken": "0x0000000000000000000000000000000000000000",  # native
                "considerationIdentifier": 0,
                "considerationAmount": int(consideration[0].get("startAmount", price_wei)) if consideration else price_wei,
                "offerer": Web3.to_checksum_address(params.get("offerer", "")),
                "zone": Web3.to_checksum_address(params.get("zone", "0x" + "0" * 40)),
                "offerToken": Web3.to_checksum_address(offer_token),
                "offerIdentifier": offer_id,
                "offerAmount": 1,
                "basicOrderType": 0,  # ETH_TO_ERC721_FULL_OPEN
                "startTime": int(params.get("startTime", 0)),
                "endTime": int(params.get("endTime", 0)),
                "zoneHash": bytes.fromhex(str(params.get("zoneHash", "0x" + "00" * 32)).replace("0x", "")),
                "salt": int(params.get("salt", 0)),
                "offererConduitKey": bytes.fromhex(str(params.get("conduitKey", "0x" + "00" * 32)).replace("0x", "")),
                "fulfillerConduitKey": bytes(32),  # no conduit for fulfiller
                "totalOriginalAdditionalRecipients": len(additional_recipients),
                "additionalRecipients": [
                    {"amount": amt, "recipient": Web3.to_checksum_address(addr)}
                    for amt, addr in additional_recipients
                ],
                "signature": bytes.fromhex(
                    str(order_data.get("signature") or params.get("signature", "0x")).replace("0x", "")
                ),
            }

            # Build tx — thread-safe nonce management
            with self._lock:
                on_chain_nonce = w3.eth.get_transaction_count(
                    Web3.to_checksum_address(self.buyer_address), "pending"
                )
                cached = self._nonce_cache.get(chain, 0)
                nonce = max(on_chain_nonce, cached)
                self._nonce_cache[chain] = nonce + 1

            # Gas estimation
            gas_price = w3.eth.gas_price
            max_gas_wei = to_gwei(self.max_gas_gwei)
            if gas_price > max_gas_wei:
                return {"success": False, "error": f"Gas too high: {gas_price / 10**9:.1f} gwei > {self.max_gas_gwei} max"}

            priority_fee = int(3 * 10**9)  # 3 gwei tip for speed
            max_fee = int(gas_price * self.gas_multiplier) + priority_fee

            try:
                tx = seaport.functions.fulfillBasicOrder(basic_params).build_transaction({
                    "from": Web3.to_checksum_address(self.buyer_address),
                    "value": total_consideration,
                    "nonce": nonce,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                    "chainId": chain_cfg["chain_id"],
                })
            except Exception as build_exc:
                # Fallback: try with legacy gas
                tx = seaport.functions.fulfillBasicOrder(basic_params).build_transaction({
                    "from": Web3.to_checksum_address(self.buyer_address),
                    "value": total_consideration,
                    "nonce": nonce,
                    "gasPrice": int(gas_price * self.gas_multiplier),
                    "chainId": chain_cfg["chain_id"],
                })

            # Estimate gas
            try:
                estimated = w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated * 1.3)  # 30% buffer
            except Exception:
                tx["gas"] = 350_000  # safe default for Seaport

            # Sign
            signed = Account.sign_transaction(tx, self.buyer_key)

            # Submit
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            explorer = chain_cfg.get("explorer", "")

            log.warning("🚀 TX SUBMITTED: %s%s", explorer, tx_hash_hex)

            # Wait for receipt (timeout 30s)
            try:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                if receipt["status"] == 1:
                    log.warning("✅ BUY SUCCESS: %s%s (gas=%d)", explorer, tx_hash_hex, receipt["gasUsed"])
                    return {
                        "success": True,
                        "tx_hash": tx_hash_hex,
                        "gas_used": receipt["gasUsed"],
                    }
                else:
                    log.error("❌ TX REVERTED: %s%s", explorer, tx_hash_hex)
                    return {"success": False, "tx_hash": tx_hash_hex, "error": "TX_REVERTED"}
            except Exception as wait_exc:
                # TX submitted but receipt timed out — do NOT mark as success
                log.warning("⏳ TX pending (no receipt in 30s): %s%s — marking as PENDING, not success", explorer, tx_hash_hex)
                return {"success": False, "tx_hash": tx_hash_hex, "error": "RECEIPT_TIMEOUT", "pending": True}

        except Exception as exc:
            log.error("Execute buy failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _fetch_order_details(self, order_id: str, chain: str) -> dict | None:
        """Fetch full order details from OKX API by orderId."""
        if not order_id:
            return None
        try:
            from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
            import base64, hashlib, hmac
            from datetime import datetime, timezone

            transport = StdlibHttpTransport(timeout=10, max_retries=1, rate_limit_per_sec=5.0)
            path = "/api/v5/mktplace/nft/markets/listings"
            params = {"chain": chain, "orderId": order_id}
            url, request_path = build_url(self.okx_api_base, path, params)

            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            message = f"{timestamp}GET{request_path}"
            digest = hmac.new(self.okx_api_secret.encode(), message.encode(), hashlib.sha256).digest()
            signature = base64.b64encode(digest).decode()

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "OK-ACCESS-KEY": self.okx_api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.okx_api_passphrase,
            }

            resp = transport.request_json(method="GET", url=url, headers=headers, body="")
            data = resp.get("data", [])
            if isinstance(data, list) and data:
                return data[0]
            return None
        except Exception as exc:
            log.debug("Fetch order details failed: %s", exc)
            return None

    # ── Telegram alert ────────────────────────────────────────────

    def _alert_buy_attempt(self, attempt: BuyAttempt):
        """Send buy attempt result to Telegram."""
        if not self.tg_token or not self.tg_chat:
            return

        chain_cfg = CHAIN_CONFIG.get(attempt.chain, {})
        explorer = chain_cfg.get("explorer", "")

        if attempt.success:
            emoji = "✅"
            status = "BOUGHT"
        elif attempt.dry_run:
            emoji = "🏷️"
            status = "DRY RUN"
        else:
            emoji = "❌"
            status = f"FAILED: {attempt.error}"

        tx_line = ""
        if attempt.tx_hash:
            tx_line = f"\nTX: <a href='{explorer}{attempt.tx_hash}'>{attempt.tx_hash[:16]}...</a>"

        msg = (
            f"{emoji} <b>Auto-Buy {status}</b>\n"
            f"Collection: {attempt.collection_name}\n"
            f"Token: #{attempt.token_id}\n"
            f"Price: {attempt.listing_price:.6f} {attempt.currency}\n"
            f"Max: {attempt.max_buy_price:.6f} {attempt.currency}\n"
            f"Chain: {attempt.chain.upper()}\n"
            f"Latency: {attempt.latency_ms}ms"
            f"{tx_line}"
        )

        try:
            from okx_nft_bot.sales_stream import http
            http.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": msg, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception:
            try:
                import urllib.request
                import json as _json
                data = _json.dumps({
                    "chat_id": self.tg_chat, "text": msg,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                }).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                log.warning("Telegram notify failed: %s", exc)
    