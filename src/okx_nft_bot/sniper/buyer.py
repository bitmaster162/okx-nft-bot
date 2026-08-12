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

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        "rpc_default": "https://ethereum-rpc.publicnode.com",
        "native": "ETH",
        "explorer": "https://etherscan.io/tx/",
        "okx_chain": "eth",
    },
    "ethereum": {
        "chain_id": 1,
        "rpc_env": "BUYER_RPC_URL_ETH",
        "rpc_default": "https://ethereum-rpc.publicnode.com",
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
        self.max_usd_per_buy = float(os.getenv("AUTO_BUY_MAX_USD", "1.0"))

        # OKX API creds (for fetching listings)
        self.okx_api_key = os.getenv("OKX_API_KEY", "")
        self.okx_api_secret = os.getenv("OKX_API_SECRET", "")
        self.okx_api_passphrase = os.getenv("OKX_API_PASSPHRASE", "")
        self.okx_api_base = os.getenv("OKX_API_BASE", "https://web3.okx.com")

        # Pre-init web3 connections (keep alive for speed)
        self._w3: dict[str, Any] = {}  # chain -> Web3 instance
        self._lock = threading.Lock()
        self._failed_orders: set[str] = set()  # orderIds that returned errors (stale/unavailable)
        self._governor: Any | None = None  # lazy-initialized ExecutionGovernor for safety + nonce allocation

        # Stats
        self.total_attempts = 0
        self.total_buys = 0
        self.total_spent: dict[str, float] = {}  # chain -> total native spent

        # Telegram
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

        # UNCONDITIONAL web3 init — counter_bidder's Phase 3 creates its own
        # OKXInstantBuyer and gates buy activity via its own `_buy_enabled`
        # (from buy_config), not via the AUTO_BUY_ENABLED env flag. If we skip
        # _init_web3() when AUTO_BUY_ENABLED is unset, every buy attempt by
        # counter_bidder fails with "Web3 not connected for chain bsc"
        # regardless of whether the buy was authorised. See feedback memory
        # "WL-first rule" + 24h log scan showing ~600 Web3-not-connected
        # errors in a pre-patch window.
        self._init_web3()
        if self.enabled:
            log.info("OKXInstantBuyer: ENABLED, effective_dry_run=%s, wallet=%s, gas_mult=%.1f, max_gas=%d gwei",
                     self._effective_dry_run(), self.buyer_address[:10] + "..." if self.buyer_address else "NONE",
                     self.gas_multiplier, self.max_gas_gwei)
        else:
            log.info("OKXInstantBuyer: DISABLED via env (set AUTO_BUY_ENABLED=1); web3 still initialised for counter_bidder use")

    def _get_governor(self):
        """Lazy-initialize ExecutionGovernor for live gating and atomic nonce allocation."""
        if self._governor is None:
            from okx_nft_bot.config import load_settings
            from okx_nft_bot.execution_governor import ExecutionGovernor
            self._governor = ExecutionGovernor(settings=load_settings())
        return self._governor

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

    def _live_buy_gate(
        self,
        *,
        collection_address: str,
        chain: str,
        price: float,
        currency: str,
    ) -> tuple[str | None, float | None, float | None]:
        """Fail-closed live gate for every capital-moving instant-buy path.

        Returns ``(blocked_reason, price_bnb_equiv, price_usd)``.  Multi-engine
        execution governance uses BNB-equivalent accounting for the shared daily
        cap, so ETH/stablecoin buys must be normalized before the governor check.
        """
        if not self.enabled:
            return "auto_buy_disabled", None, None
        if self._effective_dry_run():
            return "dry_run_enabled", None, None

        try:
            from okx_nft_bot.config import validate_execution_chain
            resolved_chain = validate_execution_chain(chain)
        except Exception as exc:
            return f"invalid_chain:{exc}", None, None

        resolved_collection = str(collection_address or "").strip().lower()
        if not resolved_collection:
            return "collection_required", None, None

        try:
            resolved_price = float(price)
        except (TypeError, ValueError):
            return "invalid_price", None, None
        if not math.isfinite(resolved_price) or resolved_price <= 0:
            return "invalid_price", None, None

        resolved_currency = str(currency or "").strip().upper()
        if not resolved_currency:
            return "currency_required", None, None

        try:
            from okx_nft_bot.prices import get_usd_price
            currency_usd = float(get_usd_price(resolved_currency))
            bnb_usd = float(get_usd_price("BNB"))
        except Exception as exc:
            return f"price_oracle_error:{type(exc).__name__}", None, None

        if (
            not math.isfinite(currency_usd)
            or not math.isfinite(bnb_usd)
            or currency_usd <= 0
            or bnb_usd <= 0
        ):
            return "price_oracle_unavailable", None, None

        price_usd = resolved_price * currency_usd
        price_bnb_equiv = price_usd / bnb_usd
        if (
            not math.isfinite(price_usd)
            or not math.isfinite(price_bnb_equiv)
            or price_usd <= 0
            or price_bnb_equiv <= 0
        ):
            return "invalid_normalized_price", None, None

        governor = self._get_governor()
        blocked = governor.check_live_submit_allowed(
            action_type="LIVE_BUY",
            collection=resolved_collection,
            chain=resolved_chain,
            price_bnb=price_bnb_equiv,
            price_usd=price_usd,
        )
        return blocked, price_bnb_equiv, price_usd

    @staticmethod
    def _record_live_buy_submit(
        governor: Any,
        *,
        collection_address: str,
        chain: str,
        price_bnb_equiv: float,
        price_usd: float,
        tx_hash: str,
    ) -> None:
        """Record a submitted buy so shared cooldown/daily caps include it.

        A transaction is already on-chain when this runs.  If persistence fails,
        force dry-run immediately so an unaccounted submit cannot be followed by
        additional capital-moving actions under stale rate-limit state.
        """
        try:
            governor.state.record_submit_event(
                engine="instant_buyer",
                action_type="LIVE_BUY",
                collection=collection_address,
                chain=chain,
                price_bnb=float(price_bnb_equiv),
                status="submitted",
                reason=f"tx_hash={tx_hash};price_usd={float(price_usd):.6f}",
            )
        except Exception as exc:
            log.critical("Instant buyer submit audit write failed after tx broadcast: %s", exc, exc_info=True)
            try:
                governor.state.set_force_dry_run(
                    True,
                    reason="instant_buyer_submit_log_failure",
                )
            except Exception as force_exc:
                log.critical(
                    "Instant buyer could not force dry-run after submit audit failure: %s",
                    force_exc,
                    exc_info=True,
                )

    @staticmethod
    def _estimate_usd(price: float, currency: str, chain: str) -> float:
        """Best-effort USD estimate for a buy price using coingecko-free or hardcoded fallback."""
        # Stablecoins
        cur = currency.upper()
        if cur in ("USDT", "USDC", "BUSD", "DAI"):
            return price
        # Try live price from coingecko
        cg_ids = {"ETH": "ethereum", "WETH": "ethereum", "BNB": "binance-coin",
                  "WBNB": "binance-coin"}
        cg_id = cg_ids.get(cur)
        if cg_id:
            try:
                import urllib.request
                url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read())
                usd_rate = data.get(cg_id, {}).get("usd", 0)
                if usd_rate > 0:
                    return price * usd_rate
            except Exception:
                pass
        # Hardcoded fallback
        fallback = {"ETH": 2100, "WETH": 2100, "BNB": 600, "WBNB": 600}
        rate = fallback.get(cur, 0)
        return price * rate if rate else price * 1000  # unknown = assume expensive

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

        # PATCH 2026-05-29 (FRESHNESS_FILTER): skip zombie listings (OKX indexer lag).
        # Phase 3 historically had 0% win over 4808+ attempts because OKX returns stale
        # listings that look active but were already sold. Only accept listings created
        # in last BUY_MAX_LISTING_AGE_S seconds (default 60). Set =0 to disable.
        _max_age_s = int(os.environ.get("BUY_MAX_LISTING_AGE_S", "60"))
        if _max_age_s > 0:
            _now_ms = int(time.time() * 1000)
            _fresh = []
            for _it in listings:
                _ts = _it.get("listingTime") or _it.get("createTime") or _it.get("listTime") or _it.get("createTs") or 0
                try:
                    _ts = int(_ts)
                    if 0 < _ts < 1e12:
                        _ts *= 1000  # was seconds
                except Exception:
                    _ts = 0
                if _ts == 0 or (_now_ms - _ts) <= _max_age_s * 1000:
                    _fresh.append(_it)
            if len(_fresh) < len(listings):
                log.info("FRESHNESS_FILTER %s: %d/%d listings within %ds, %d filtered as zombie",
                         collection_name, len(_fresh), len(listings), _max_age_s, len(listings)-len(_fresh))
            listings = _fresh
            if not listings:
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

        # USD safety cap: estimate buy cost in USD and reject if above limit
        usd_estimate = self._estimate_usd(cheapest_price, currency, chain_lower)
        if usd_estimate > self.max_usd_per_buy:
            log.warning("🛑 %s #%s @ %.6f %s (~$%.2f) EXCEEDS safety cap $%.2f — SKIP",
                        collection_name, token_id, cheapest_price, currency,
                        usd_estimate, self.max_usd_per_buy)
            return None

        log.warning("🎯🔥 BUY TARGET: %s #%s @ %.6f %s (~$%.2f, cap=$%.2f) [%s]",
                    collection_name, token_id, cheapest_price, currency,
                    usd_estimate, self.max_usd_per_buy, chain_lower)

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
            result = self._execute_buy(
                cheapest,
                chain_lower,
                cheapest_price,
                collection_address=collection_address,
                currency=currency,
            )
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

    def _execute_buy(
        self,
        listing: dict,
        chain: str,
        price: float,
        *,
        collection_address: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Execute buy via OKX v5 API → on-chain tx.

        Every call, including direct/internal calls that bypass ``try_buy()``, is
        gated through ExecutionGovernor before nonce allocation/sign/broadcast.
        """
        chain = str(chain or "").strip().lower()
        if chain == "ethereum":
            chain = "eth"

        resolved_collection = str(
            collection_address
            or listing.get("collectionAddress")
            or listing.get("contractAddress")
            or listing.get("collection_address")
            or ""
        ).strip().lower()
        resolved_currency = str(
            currency
            or listing.get("currency")
            or listing.get("currencySymbol")
            or CHAIN_CONFIG.get(chain, {}).get("native", "")
        ).strip().upper()

        blocked, price_bnb_equiv, price_usd = self._live_buy_gate(
            collection_address=resolved_collection,
            chain=chain,
            price=price,
            currency=resolved_currency,
        )
        if blocked:
            log.warning(
                "LIVE BUY blocked before execution for %s [%s]: %s",
                resolved_collection or "unknown",
                chain or "unknown",
                blocked,
            )
            return {"success": False, "error": f"LIVE_GATE_BLOCKED:{blocked}"}

        if not self.buyer_address or not self.buyer_key:
            return {"success": False, "error": "BUYER_WALLET not configured"}

        order_id = listing.get("orderId") or listing.get("orderHash") or ""
        if not order_id:
            return {"success": False, "error": "No orderId in listing data"}

        if order_id in self._failed_orders:
            return {"success": False, "error": f"Order {order_id} already failed (stale/unavailable)"}

        w3 = self._get_w3(chain)
        if w3 is None:
            return {"success": False, "error": f"Web3 not connected for chain {chain}"}

        try:
            from web3 import Web3
            from eth_account import Account
            from okx_nft_bot.counterbid.okx_api import OKXAPIClient
            from okx_nft_bot.config import load_settings

            # Step 1: Get tx calldata from OKX API
            settings = load_settings()
            client = OKXAPIClient(settings=settings)

            buy_data = client.buy_listing(
                chain=chain,
                wallet_address=self.buyer_address,
                order_id=order_id,
            )

            contract_addr = buy_data.get("contract_address", "")
            input_data = buy_data.get("input", "")
            value_str = buy_data.get("value", "0")

            if not contract_addr or not input_data:
                return {"success": False, "error": f"OKX buy API returned no calldata: {buy_data}"}

            if buy_data.get("approval_needed"):
                return {"success": False, "error": "ERC20 approval needed — not yet automated for buy"}

            # Re-check after fetching OKX calldata. This closes the window where
            # live arm can expire or a killswitch/rate limit can become active
            # during the marketplace API request.
            governor = self._get_governor()
            final_blocked = governor.check_live_submit_allowed(
                action_type="LIVE_BUY",
                collection=resolved_collection,
                chain=chain,
                price_bnb=float(price_bnb_equiv or 0.0),
                price_usd=float(price_usd or 0.0),
            )
            if final_blocked:
                return {"success": False, "error": f"LIVE_GATE_BLOCKED:{final_blocked}"}

            # Step 2: Build on-chain transaction and complete all network-side
            # gas checks before reserving a local nonce.
            chain_cfg = CHAIN_CONFIG[chain]
            value_wei = int(value_str) if value_str.isdigit() else int(float(value_str) * 10**18)

            gas_price = w3.eth.gas_price
            max_gas_wei = int(self.max_gas_gwei * 10**9)
            if gas_price > max_gas_wei:
                return {"success": False, "error": f"Gas too high: {gas_price / 10**9:.1f} gwei > {self.max_gas_gwei} max"}

            tx: dict[str, Any] = {
                "from": Web3.to_checksum_address(self.buyer_address),
                "to": Web3.to_checksum_address(contract_addr),
                "data": input_data,
                "value": value_wei,
                "chainId": chain_cfg["chain_id"],
            }

            # Gas: try EIP-1559, fall back to legacy
            try:
                priority_fee = int(3 * 10**9)
                max_fee = int(gas_price * self.gas_multiplier) + priority_fee
                tx["maxFeePerGas"] = max_fee
                tx["maxPriorityFeePerGas"] = priority_fee
            except Exception:
                tx["gasPrice"] = int(gas_price * self.gas_multiplier)

            try:
                estimated = w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated * 1.3)
            except Exception as gas_exc:
                log.warning("Gas estimate failed: %s — using 350k", gas_exc)
                tx["gas"] = 350_000

            # Final fail-closed check after all network/gas work and immediately
            # before nonce reservation. From this point to broadcast there are no
            # external waits, which minimizes disarm/killswitch TOCTOU and avoids
            # consuming a local nonce when the final gate blocks.
            broadcast_blocked = governor.check_live_submit_allowed(
                action_type="LIVE_BUY",
                collection=resolved_collection,
                chain=chain,
                price_bnb=float(price_bnb_equiv or 0.0),
                price_usd=float(price_usd or 0.0),
            )
            if broadcast_blocked:
                return {"success": False, "error": f"LIVE_GATE_BLOCKED:{broadcast_blocked}"}

            nonce = governor.allocate_nonce(self.buyer_address, chain)
            tx["nonce"] = nonce

            # Step 3: Sign and submit
            signed = Account.sign_transaction(tx, self.buyer_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            explorer = chain_cfg.get("explorer", "")

            self._record_live_buy_submit(
                governor,
                collection_address=resolved_collection,
                chain=chain,
                price_bnb_equiv=float(price_bnb_equiv or 0.0),
                price_usd=float(price_usd or 0.0),
                tx_hash=tx_hash_hex,
            )

            log.warning("🚀 BUY TX SUBMITTED: %s%s", explorer, tx_hash_hex)

            # Wait for receipt
            try:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                if receipt["status"] == 1:
                    log.warning("✅ BUY SUCCESS: %s%s (gas=%d)", explorer, tx_hash_hex, receipt["gasUsed"])
                    return {"success": True, "tx_hash": tx_hash_hex, "gas_used": receipt["gasUsed"]}
                else:
                    log.error("❌ BUY TX REVERTED: %s%s", explorer, tx_hash_hex)
                    return {"success": False, "tx_hash": tx_hash_hex, "error": "TX_REVERTED"}
            except Exception:
                log.warning("⏳ BUY TX pending (no receipt in 30s): %s%s", explorer, tx_hash_hex)
                return {"success": False, "tx_hash": tx_hash_hex, "error": "RECEIPT_TIMEOUT", "pending": True}

        except Exception as exc:
            log.error("Execute buy failed: %s", exc, exc_info=True)
            self._failed_orders.add(order_id)
            return {"success": False, "error": str(exc)}

    def _fetch_order_details(self, order_id: str, chain: str) -> dict | None:
        """Fetch full order details from OKX API by orderId."""
        if not order_id:
            return None
        try:
            from okx_nft_bot.clients.http import StdlibHttpTransport, build_url

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

        # Skip TG for in-memory dedup short-circuits: _failed_orders cache
        # re-emits the same "already failed (stale/unavailable)" error every
        # cycle the zombie listing is re-discovered; we still log it but don't
        # spam Telegram with duplicates.
        if attempt.error and "already failed (stale" in attempt.error:
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
                data = json.dumps({
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
