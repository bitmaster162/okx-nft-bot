"""
Real-time Sales Stream Daemon — Rival-Undercutter v17+

Continuously polls OKX, OpenSea, and MagicEden for NFT sales/trades,
normalizes them into a unified format, and writes to SQLite.

Designed to run as a standalone daemon or integrate into okx-nft-bot.

Usage:
    python sales_stream_daemon.py                    # run forever
    python sales_stream_daemon.py --once             # single pass
    python sales_stream_daemon.py --interval 15      # poll every 15s
    python sales_stream_daemon.py --markets okx,opensea  # specific markets only

Env vars (from .env):
    OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
    OPENSEA_API_KEY
    MAGICEDEN_API_KEY (optional)
    SALES_DB_PATH=./data/sales_stream.sqlite3
    SALES_POLL_INTERVAL=30
    SALES_MARKETS=okx,opensea,magiceden
    RIVAL_WALLETS=0x...,0x...
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional, for alerts)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import time
from base64 import b64encode
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.currency import canonical_currency

# Playwright stream (lazy import — only needed for sales-stream service)
_pw_stream_available = False
try:
    from okx_nft_bot.playwright_okx_stream import SyncPlaywrightOKXStream
    _pw_stream_available = True
except ImportError:
    pass

# Stream filters (hot-reloadable)
try:
    from okx_nft_bot.stream_filters import StreamFilters
except ImportError:
    StreamFilters = None  # type: ignore[assignment,misc]

# Use curl_cffi for Windows SSL/IPv6 compatibility (same as main bot)
try:
    from curl_cffi import requests as http
    HTTP_ENGINE = "curl_cffi"
except ImportError:
    import requests as http  # type: ignore[no-redef]
    HTTP_ENGINE = "requests"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Also log to file for external inspection (flush every write)
class _FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()
_file_handler = _FlushFileHandler("./data/bot.log", mode="a", encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_file_handler)

# Small debug log (max 1MB, rotated) — syncs fast via Google Drive
from logging.handlers import RotatingFileHandler
_debug_handler = RotatingFileHandler(
    "./data/debug.log", maxBytes=1_000_000, backupCount=1,
    encoding="utf-8",
)
_debug_handler.setLevel(logging.INFO)
_debug_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
# Only capture counter_bidder, counterbid, mass_offer logs
for _logger_name in ("sniper.counter_bidder", "counterbid.okx_api", "okx_nft_bot.mass_offer.engine"):
    logging.getLogger(_logger_name).addHandler(_debug_handler)

log = logging.getLogger("sales_stream")

# ─── Log Snapshot Thread ───────────────────────────────────────
# Every 5 minutes, copy debug.log / bot.log → frozen snapshot files.
# Snapshot files don't update between copies → Google Drive syncs them
# without locking issues.  Only 1 copy kept (overwrite), no garbage.
import shutil
import threading

class _LogSnapshotThread(threading.Thread):
    """Daemon thread: periodically snapshots active log files."""

    INTERVAL = 300  # 5 minutes
    PAIRS = [
        ("./data/debug.log", "./data/debug_snapshot.log"),
        ("./data/bot.log",   "./data/bot_snapshot.log"),
    ]

    def __init__(self):
        super().__init__(daemon=True, name="log-snapshot")

    def run(self):
        while True:
            time.sleep(self.INTERVAL)
            for src, dst in self.PAIRS:
                try:
                    if os.path.exists(src):
                        shutil.copy2(src, dst)
                except Exception as exc:
                    log.debug("Log snapshot copy failed for %s -> %s: %s", src, dst, exc)

_LogSnapshotThread().start()


# ─── Data Models ────────────────────────────────────────────────

@dataclass
class SaleEvent:
    """Normalized sale event from any marketplace."""
    event_id: str               # unique hash
    market: str                 # okx | opensea | magiceden
    chain: str                  # bsc | ethereum | polygon | solana
    collection_address: str
    collection_name: str
    token_id: str
    price: float                # in native token (BNB/ETH/SOL)
    price_usd: float | None     # if available
    currency: str               # BNB | ETH | WETH | SOL
    seller: str
    buyer: str
    tx_hash: str
    block_number: int | None
    timestamp: str              # ISO 8601 UTC
    is_rival_seller: bool    # seller is in RIVAL_WALLETS
    is_rival_buyer: bool     # buyer is in RIVAL_WALLETS
    trade_type: str             # buy | listing | offer | unknown
    raw_json: str               # original API response for this event


# ─── Database ───────────────────────────────────────────────────

class SalesDatabase:
    def __init__(self, db_path: str = "./data/sales_stream.sqlite3"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sales (
                    event_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    collection_address TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    price_usd REAL,
                    currency TEXT NOT NULL,
                    seller TEXT NOT NULL,
                    buyer TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    block_number INTEGER,
                    timestamp TEXT NOT NULL,
                    is_rival_seller INTEGER NOT NULL DEFAULT 0,
                    is_rival_buyer INTEGER NOT NULL DEFAULT 0,
                    trade_type TEXT NOT NULL DEFAULT 'покупка',
                    raw_json TEXT,
                    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_market_ts ON sales(market, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_collection ON sales(collection_address, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_rival ON sales(is_rival_seller, is_rival_buyer)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_seller ON sales(seller)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_buyer ON sales(buyer)"
            )
            # Migrate: add trade_type column if missing
            try:
                conn.execute("ALTER TABLE sales ADD COLUMN trade_type TEXT NOT NULL DEFAULT 'покупка'")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Cursor state for each market
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stream_cursors (
                    market TEXT PRIMARY KEY,
                    cursor_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def save_events(self, events: list[SaleEvent]) -> int:
        """Insert events, skip duplicates. Returns count of new events."""
        if not events:
            return 0
        inserted = 0
        with self._connect() as conn:
            for e in events:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO sales (
                            event_id, market, chain, collection_address, collection_name,
                            token_id, price, price_usd, currency, seller, buyer,
                            tx_hash, block_number, timestamp,
                            is_rival_seller, is_rival_buyer, trade_type, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        e.event_id, e.market, e.chain, e.collection_address,
                        e.collection_name, e.token_id, e.price, e.price_usd,
                        e.currency, e.seller, e.buyer, e.tx_hash, e.block_number,
                        e.timestamp, int(e.is_rival_seller), int(e.is_rival_buyer),
                        e.trade_type, e.raw_json,
                    ))
                    if conn.execute("SELECT changes()").fetchone()[0] > 0:
                        inserted += 1
                except sqlite3.Error as exc:
                    log.warning("DB insert error for %s: %s", e.event_id, exc)
        return inserted

    def get_cursor(self, market: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor_value FROM stream_cursors WHERE market = ?", (market,)
            ).fetchone()
        return row[0] if row else None

    def set_cursor(self, market: str, value: str):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO stream_cursors (market, cursor_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(market) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    updated_at = CURRENT_TIMESTAMP
            """, (market, value))

    def get_stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            by_market = dict(conn.execute(
                "SELECT market, COUNT(*) FROM sales GROUP BY market"
            ).fetchall())
            rival_sales = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE is_rival_seller = 1 OR is_rival_buyer = 1"
            ).fetchone()[0]
            latest = conn.execute(
                "SELECT market, MAX(timestamp) FROM sales GROUP BY market"
            ).fetchall()
        return {
            "total_sales": total,
            "by_market": by_market,
            "rival_involved": rival_sales,
            "latest_by_market": dict(latest),
        }


# ─── OKX chain-id mapping (priapi uses numeric IDs) ───────────
# Known OKX priapi chain IDs.  Extend as needed.
OKX_CHAIN_IDS: dict[str, str] = {
    "eth": "1",
    "ethereum": "1",
    "bsc": "56",        # BNB Smart Chain (standard chainId)
    "polygon": "137",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
}

# ─── OKX API Client ────────────────────────────────────────────

class OKXSalesClient:
    """Polls OKX Web3 NFT marketplace for recent trades.

    Uses the public ``priapi`` global activity feed
    (``/priapi/v1/nft/trading/collectionHistory``) which returns ALL
    sales across every collection on the chosen chain — no need to
    iterate over individual collections.

    Falls back to the authenticated v5 per-collection endpoint
    (``/api/v5/mktplace/nft/markets/trades``) when *collection_address*
    is explicitly provided.
    """

    def __init__(self, api_key: str = "", api_secret: str = "",
                 passphrase: str = "",
                 base_url: str = "https://web3.okx.com",
                 chain: str = "bsc",
                 okx_chain_id: str | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.chain = chain
        # Numeric chain ID for the priapi endpoint
        self.chain_id = okx_chain_id or OKX_CHAIN_IDS.get(chain.lower(), "")

    # ── authenticated v5 helpers (kept for backward compat) ──

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        msg = f"{timestamp}{method}{path}{body}"
        return b64encode(
            hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    # ── Global feed (priapi — requires OK-VERIFY-SIGN) ─────

    def _priapi_headers(self, method: str, path: str) -> dict[str, str]:
        """Build headers for priapi endpoints.

        The priapi requires ``OK-VERIFY-TOKEN`` — a session/device token
        obtained from the OKX web frontend.  Set it via the
        ``OKX_VERIFY_TOKEN`` env-var.  When the token is missing or
        expired the caller should fall back to the authenticated v5 API.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        headers: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://web3.okx.com/ru/nft/activity",
        }
        # priapi session headers (from browser DevTools)
        verify_token = os.getenv("OKX_VERIFY_TOKEN", "")
        verify_sign = os.getenv("OKX_VERIFY_SIGN", "")
        devid = os.getenv("OKX_DEVID", "")
        if verify_token:
            headers["ok-verify-token"] = verify_token
        if verify_sign:
            headers["ok-verify-sign"] = verify_sign
        if devid:
            headers["devid"] = devid
        # Standard OKX auth headers (may also be needed)
        if self.api_key and self.api_secret and self.passphrase:
            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-PASSPHRASE"] = self.passphrase
            sig = self._sign(ts, method, path)
            headers["OK-ACCESS-SIGN"] = sig
        return headers

    def fetch_global_sales(self, *, time_boundary: str | None = None,
                           address: str = "",
                           ) -> tuple[list[dict], str | None]:
        """Fetch ALL recent NFT sales on the chain via the priapi.

        Returns ``(trades_list, next_time_boundary)`` where
        *next_time_boundary* can be passed in the next call for pagination.
        """
        path = "/priapi/v1/nft/trading/collectionHistory"
        url = f"{self.base_url}{path}"
        params: dict[str, str] = {
            "showCollOrder": "true",
            "type": "SALE",
            "chain": self.chain_id,
            "project": "",          # empty = ALL collections
            "address": address,     # empty = ALL wallets; set to filter by wallet
            "direction": "1",       # newest first
            "t": str(int(time.time() * 1000)),
        }
        if time_boundary:
            params["timeBoundary"] = time_boundary

        # Build full request path with query string for signature
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        full_path = f"{path}?{query}"
        headers = self._priapi_headers("GET", full_path)

        try:
            resp = http.get(url, params=params, headers=headers, timeout=20)
            data = resp.json()
            code = data.get("code", -1)
            if code != 0 and str(code) != "0":
                log.warning("OKX priapi error: code=%s msg=%s",
                            code, data.get("msg", data.get("error_message", "")))
                return [], None

            result = data.get("data", {})
            # The response structure may vary — handle both list and dict
            if isinstance(result, list):
                trades = result
            elif isinstance(result, dict):
                trades = result.get("data", result.get("list", result.get("items", [])))
            else:
                trades = []

            # Derive next cursor from the oldest trade's timestamp
            next_cursor: str | None = None
            if trades:
                last = trades[-1]
                # Try common timestamp fields
                ts_val = (last.get("tradedTime") or last.get("createTime")
                          or last.get("timestamp") or last.get("createDate") or "")
                if ts_val:
                    # Convert ms → seconds if needed
                    ts_num = str(ts_val)
                    if len(ts_num) > 12:
                        ts_num = ts_num[:10]
                    next_cursor = ts_num

            log.info("OKX priapi global feed: %d trades, chain=%s",
                     len(trades), self.chain_id)
            return trades, next_cursor

        except Exception as exc:
            log.error("OKX priapi fetch failed: %s", exc)
            return [], None

    # ── Per-collection v5 endpoint (legacy, kept as fallback) ─

    def fetch_recent_trades(self, *, collection_address: str,
                            cursor: str | None = None,
                            limit: int = 50) -> tuple[list[dict], str | None]:
        """Fetch recent NFT trades for a specific collection (v5 auth API)."""
        path = "/api/v5/mktplace/nft/markets/trades"
        params = {
            "chain": self.chain,
            "collectionAddress": collection_address,
            "limit": str(limit),
        }
        if cursor:
            params["cursor"] = cursor

        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        full_path = f"{path}?{query}"

        try:
            resp = http.get(
                f"{self.base_url}{full_path}",
                headers=self._headers("GET", full_path),
                timeout=20,
            )
            # Handle HTTP-level rate limit (429)
            status = getattr(resp, "status_code", 200)
            if status == 429:
                log.warning("OKX v5 rate-limited (429) for %s", collection_address)
                return [], None

            data = resp.json()
            if str(data.get("code", "")) != "0":
                msg = data.get("msg", "")
                if "Too Many" in str(msg):
                    log.warning("OKX v5 app-level rate limit for %s: %s",
                                collection_address, msg)
                    return [], None
                log.warning("OKX trades API error for %s: %s",
                            collection_address, msg or data)
                return [], None

            trades = data.get("data", {}).get("data", [])
            next_cursor = data.get("data", {}).get("cursor")
            return trades, next_cursor

        except Exception as exc:
            log.error("OKX trades fetch failed for %s: %s", collection_address, exc)
            return [], None

    @staticmethod
    def _parse_trade_type(trade: dict) -> str:
        """Parse trade type from priapi type/typeName fields."""
        # priapi v2: type=53 typeName="Купить" (buy), type=52 typeName="Листинг" (listing)
        # type=54 = offer/accept offer
        type_code = str(trade.get("type", ""))
        type_name = (trade.get("typeName") or "").lower()
        if type_code == "53" or "купить" in type_name or "buy" in type_name:
            return "покупка"
        elif type_code == "52" or "листинг" in type_name or "list" in type_name:
            return "листинг"
        elif type_code == "54" or "оффер" in type_name or "offer" in type_name or "accept" in type_name:
            return "офф"
        return type_name or "трейд"

    def normalize(self, trade: dict, rival_wallets: set[str]) -> SaleEvent:
        """Normalize a trade dict from either priapi or v5 endpoint."""
        # priapi fields:  fromAddr / toAddr / tradedTime / collAddr / collName / nftId / tradePrice / txHash
        # v5 fields:      fromAddress / toAddress / createDate / collectionAddress / collectionName / tokenId / price / txId

        # priapi v2 fields: from/to/price/contractAddress/projectName/tokenId/txId/createOn
        seller = (trade.get("fromAddr") or trade.get("from") or trade.get("fromAddress") or "").lower()
        buyer = (trade.get("toAddr") or trade.get("to") or trade.get("toAddress") or "").lower()
        # "from"/"to" can be dicts with address field in priapi v2
        if isinstance(seller, dict):
            seller = (seller.get("address") or "").lower()
        if isinstance(buyer, dict):
            buyer = (buyer.get("address") or "").lower()

        price = float(trade.get("tradePrice") or trade.get("price") or 0)
        ts = (trade.get("tradedTime") or trade.get("createOn") or trade.get("createDate")
              or trade.get("createTime") or trade.get("timestamp") or "")
        if ts and str(ts).isdigit():
            ts_int = int(ts)
            if ts_int > 1e12:
                ts_int = ts_int // 1000
            ts = datetime.fromtimestamp(ts_int, tz=timezone.utc).isoformat()

        tx = trade.get("txHash") or trade.get("txId") or ""
        token_id = str(trade.get("nftId") or trade.get("tokenId") or "")

        event_id = hashlib.sha256(
            f"okx:{tx}{token_id}".encode()
        ).hexdigest()[:24]

        collection_addr = (
            trade.get("collAddr") or trade.get("contractAddress")
            or trade.get("collectionAddress") or ""
        ).lower()
        collection_name = (
            trade.get("projectName") or trade.get("collName")
            or trade.get("collectionName") or ""
        )

        # Use chain tag from Playwright if present, otherwise default
        trade_chain = trade.get("_pw_chain") or self.chain

        return SaleEvent(
            event_id=event_id,
            market="okx",
            chain=trade_chain,
            collection_address=collection_addr,
            collection_name=collection_name,
            token_id=token_id,
            price=price,
            price_usd=float(trade["priceUsd"]) if trade.get("priceUsd") else None,
            currency=canonical_currency(trade.get("currency") or trade.get("currencyName") or trade.get("tokenSymbol") or "ETH") or "ETH",
            seller=seller,
            buyer=buyer,
            tx_hash=tx,
            block_number=int(trade["blockNumber"]) if trade.get("blockNumber") else None,
            timestamp=ts,
            is_rival_seller=seller in rival_wallets,
            is_rival_buyer=buyer in rival_wallets,
            trade_type=self._parse_trade_type(trade),
            raw_json=json.dumps(trade, ensure_ascii=False),
        )


# ─── OpenSea API Client ────────────────────────────────────────

class OpenSeaSalesClient:
    """Polls OpenSea Stream/Events API for sales."""

    def __init__(self, api_key: str, chain: str = "ethereum"):
        self.api_key = api_key
        self.base_url = "https://api.opensea.io/api/v2"
        self.chain = chain

    def fetch_recent_sales(self, *, after: str | None = None,
                           limit: int = 50) -> tuple[list[dict], str | None]:
        """Fetch recent sales events. Returns (events, next_cursor)."""
        params: dict[str, Any] = {
            "event_type": "sale",
            "limit": limit,
        }
        if after:
            params["after"] = after

        try:
            resp = http.get(
                f"{self.base_url}/events",
                params=params,
                headers={
                    "X-API-KEY": self.api_key,
                    "Accept": "application/json",
                },
                timeout=20,
            )
            data = resp.json()
            events = data.get("asset_events", [])
            next_cursor = data.get("next")
            return events, next_cursor

        except Exception as exc:
            log.error("OpenSea sales fetch failed: %s", exc)
            return [], None

    def normalize(self, event: dict, rival_wallets: set[str]) -> SaleEvent:
        seller = (event.get("seller") or event.get("from_address") or "").lower()
        buyer = (event.get("winner_account", {}).get("address") or
                 event.get("to_address") or "").lower()

        payment = event.get("payment", {})
        price_raw = int(payment.get("quantity") or 0)
        decimals = int(payment.get("decimals") or 18)
        price = price_raw / (10 ** decimals) if price_raw else 0.0

        nft = event.get("nft", {})
        ts = event.get("event_timestamp") or event.get("created_date") or ""

        event_id = hashlib.sha256(
            f"opensea:{event.get('order_hash','')}{nft.get('identifier','')}".encode()
        ).hexdigest()[:24]

        return SaleEvent(
            event_id=event_id,
            market="opensea",
            chain=nft.get("chain") or self.chain,
            collection_address=(nft.get("contract") or "").lower(),
            collection_name=nft.get("collection") or "",
            token_id=str(nft.get("identifier") or ""),
            price=price,
            price_usd=None,
            currency=canonical_currency(payment.get("symbol") or "ETH") or "ETH",
            seller=seller,
            buyer=buyer,
            tx_hash=event.get("transaction") or "",
            block_number=None,
            timestamp=ts,
            is_rival_seller=seller in rival_wallets,
            is_rival_buyer=buyer in rival_wallets,
            trade_type="покупка",
            raw_json=json.dumps(event, ensure_ascii=False),
        )


# ─── MagicEden API Client ──────────────────────────────────────

class MagicEdenSalesClient:
    """Polls MagicEden API for recent sales.

    DEPRECATED: MagicEden shut down EVM support (Ethereum, BSC, Polygon) in March 2025.
    This client is non-functional for EVM chains and will return 0 results.
    Kept for code stability; monitoring will show 0 events from MagicEden EVM markets.
    """

    def __init__(self, api_key: str = "", chain: str = "bsc"):
        self.api_key = api_key
        self.base_url = "https://api-mainnet.magiceden.dev/v3/rtp"
        self.chain = chain

    def fetch_recent_sales(self, *, contract: str,
                           continuation: str | None = None,
                           limit: int = 50) -> tuple[list[dict], str | None]:
        """Fetch recent sales for a specific contract. Returns (sales, next_continuation)."""
        chain_slug = self.chain

        params: dict[str, Any] = {
            "contract": contract,
            "limit": limit,
            "sortBy": "updatedAt",
            "sortDirection": "desc",
        }
        if continuation:
            params["continuation"] = continuation

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = http.get(
                f"{self.base_url}/{chain_slug}/sales/v6",
                params=params,
                headers=headers,
                timeout=20,
            )
            if resp.status_code != 200:
                log.warning("MagicEden sales API %d for %s: %s",
                            resp.status_code, contract, resp.text[:200])
                return [], None
            data = resp.json()
            sales = data.get("sales", [])
            next_cont = data.get("continuation")
            return sales, next_cont

        except Exception as exc:
            log.error("MagicEden sales fetch failed for %s: %s", contract, exc)
            return [], None

    def normalize(self, sale: dict, rival_wallets: set[str]) -> SaleEvent:
        seller = (sale.get("from") or "").lower()
        buyer = (sale.get("to") or "").lower()
        price_raw = sale.get("price", {})
        if isinstance(price_raw, dict):
            price = float(price_raw.get("amount", {}).get("decimal") or 0)
            currency = canonical_currency(price_raw.get("currency", {}).get("symbol") or "BNB") or "BNB"
        else:
            price = float(price_raw or 0)
            currency = canonical_currency("BNB") or "BNB"

        ts = sale.get("updatedAt") or sale.get("timestamp") or ""
        token = sale.get("token", {})

        event_id = hashlib.sha256(
            f"magiceden:{sale.get('txHash','')}{token.get('tokenId','')}".encode()
        ).hexdigest()[:24]

        return SaleEvent(
            event_id=event_id,
            market="magiceden",
            chain=self.chain,
            collection_address=(token.get("contract") or sale.get("contract") or "").lower(),
            collection_name=token.get("collection", {}).get("name") or "",
            token_id=str(token.get("tokenId") or ""),
            price=price,
            price_usd=float(sale["priceUSD"]) if sale.get("priceUSD") else None,
            currency=currency,
            seller=seller,
            buyer=buyer,
            tx_hash=sale.get("txHash") or "",
            block_number=int(sale["block"]) if sale.get("block") else None,
            timestamp=ts,
            is_rival_seller=seller in rival_wallets,
            is_rival_buyer=buyer in rival_wallets,
            trade_type="покупка",
            raw_json=json.dumps(sale, ensure_ascii=False),
        )


# ─── Telegram Alerter ──────────────────────────────────────────

class TelegramAlerter:
    """Send alerts when rival activity is detected in sales."""

    _TYPE_ICONS = {
        "\u043f\u043e\u043a\u0443\u043f\u043a\u0430": "\U0001f6d2",
        "\u043b\u0438\u0441\u0442\u0438\u043d\u0433": "\U0001f3f7\ufe0f",
        "\u043e\u0444\u0444":     "\U0001f48e",
    }

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    def alert_rival_sale(self, event: SaleEvent):
        if not self.enabled:
            return
        role = "SELLER" if event.is_rival_seller else "BUYER"
        address = event.seller if event.is_rival_seller else event.buyer
        msg = (
            f"🚨 <b>Rival {role} Detected</b>\n"
            f"Market: {event.market.upper()}\n"
            f"Collection: {event.collection_name}\n"
            f"Token: #{event.token_id}\n"
            f"Price: {event.price:.4f} {event.currency}\n"
            f"Wallet: <code>{address[:8]}...{address[-6:]}</code>\n"
            f"TX: <code>{event.tx_hash[:16]}...</code>\n"
            f"Time: {event.timestamp}"
        )
        try:
            http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Telegram alert failed: %s", exc)

    def alert_binance_trade(self, event: SaleEvent, wl_info: dict,
                            buy_limits: dict | None = None):
        """Send Telegram alert for a trade in a Binance-whitelisted collection."""
        if not self.enabled:
            return
        list_type = wl_info.get("binance_list_type", "?")
        bn_name = wl_info.get("collection_name", event.collection_name)

        type_icon = self._TYPE_ICONS.get(event.trade_type, "\U0001f4ca")

        # Signal emoji (price vs buy limits)
        signal = ""
        if buy_limits:
            max_buy = buy_limits.get("max_buy_price", 0)
            max_offer = buy_limits.get("max_offer_price", 0)
            if max_buy and event.price <= max_buy:
                signal = "🟢 "
            elif max_offer and event.price <= max_offer * 1.5:
                signal = "🟡 "
            else:
                signal = "🔴 "

        # USD price if available
        usd_str = f" (${event.price_usd:.2f})" if event.price_usd else ""
        # Max price from config
        max_buy_str = ""
        if buy_limits:
            mb = buy_limits.get("max_buy_price", 0)
            if mb:
                max_buy_str = f" | max: {mb}"

        # OKX collection link
        okx_link = f"https://web3.okx.com/nft/collection/{event.chain}/{event.collection_address}"

        msg = (
            f"{type_icon} {signal}{bn_name} | {event.trade_type}\n"
            f"{event.price:.4f} {event.currency}{usd_str}{max_buy_str}\n"
            f"{event.chain.upper()} | {event.market.upper()}\n"
            f"<a href=\"{okx_link}\">🔗 OKX</a>"
        )
        try:
            http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Telegram binance alert failed: %s", exc)

    def send_summary(self, cycle: int, total_trades: int, rival_count: int,
                     chains_active: int, by_chain: dict[str, int]):
        """Send periodic summary to Telegram."""
        if not self.enabled:
            return
        chain_lines = "\n".join(
            f"  {chain.upper()}: {count}" for chain, count in sorted(by_chain.items())
        ) or "  (none)"
        emoji = "🟢" if total_trades > 0 else "🔴"
        rival_emoji = "🚨" if rival_count > 0 else "✅"
        msg = (
            f"{emoji} <b>Sales Stream Summary</b> (cycle {cycle})\n"
            f"Trades this cycle: {total_trades}\n"
            f"Chains active: {chains_active}\n"
            f"By chain:\n{chain_lines}\n"
            f"{rival_emoji} Rival hits: {rival_count}"
        )
        try:
            http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Telegram summary failed: %s", exc)


# ─── Main Daemon ────────────────────────────────────────────────

class SalesStreamDaemon:
    """Continuously polls all markets and writes sales to SQLite.

    Loads collections from the bot's registry (config/collections_registry.json)
    and polls each enabled collection on its respective market.
    """

    _CHAIN_DEFAULTS = {
        ("bsc", "BNB"): 0.0009, ("bsc", "WBNB"): 0.0009,
        ("eth", "ETH"): 0.000257, ("eth", "WETH"): 0.000257,
    }

    def __init__(self, *, db_path: str | None = None, interval: int | None = None,
                 markets_csv: str | None = None, registry_path: str | None = None):
        _db_path = db_path or os.getenv("SALES_DB_PATH", "./data/sales_stream.sqlite3")
        self.db = SalesDatabase(_db_path)
        self.rival_wallets = self._load_rival_wallets()
        self.interval = interval or int(os.getenv("SALES_POLL_INTERVAL", "30"))
        self.markets = (markets_csv or os.getenv("SALES_MARKETS", "okx,opensea,magiceden")).lower().split(",")

        # Load collections registry
        self.collections = self._load_registry(
            registry_path or os.getenv("REGISTRY_PATH", "./config/collections_registry.json")
        )

        # Init clients
        self.clients: dict[str, Any] = {}

        # Playwright-based global priapi stream (preferred for OKX)
        self._pw_stream: SyncPlaywrightOKXStream | None = None
        self._use_playwright = os.getenv("OKX_USE_PLAYWRIGHT", "1").lower() in ("1", "true", "yes")
        self.collection_blacklist = self._load_blacklist()
        self.binance_whitelist = self._load_binance_whitelist()
        self.buy_config = self._load_buy_config()

        # Hot-reloadable stream filters
        self.filters = StreamFilters() if StreamFilters else None

        if "okx" in self.markets:
            chain = os.getenv("OKX_CHAIN", "bsc")
            okx_chain_id = os.getenv("OKX_CHAIN_ID", "")  # override auto-detect
            self.clients["okx"] = OKXSalesClient(
                api_key=os.getenv("OKX_API_KEY", ""),
                api_secret=os.getenv("OKX_API_SECRET", ""),
                passphrase=os.getenv("OKX_API_PASSPHRASE", ""),
                chain=chain,
                okx_chain_id=okx_chain_id or None,
            )

            # Try to start Playwright priapi stream (single tab, ALL chains)
            if self._use_playwright and _pw_stream_available:
                try:
                    self._pw_stream = SyncPlaywrightOKXStream()
                    self._pw_stream.start()
                    log.info("OKX client: Playwright priapi stream ACTIVE (ALL chains, global feed)")
                except Exception as exc:
                    log.warning("Playwright stream failed to start: %s — falling back to v5 per-collection", exc)
                    self._pw_stream = None
            else:
                reason = "disabled by OKX_USE_PLAYWRIGHT=0" if not self._use_playwright else "playwright not installed"
                log.info("OKX client: chain=%s, chain_id=%s, mode=v5-per-collection (%s)",
                         chain, self.clients["okx"].chain_id, reason)

        if "opensea" in self.markets:
            os_key = os.getenv("OPENSEA_API_KEY", "")
            if os_key:
                self.clients["opensea"] = OpenSeaSalesClient(
                    os_key, chain=os.getenv("OPENSEA_CHAIN", "ethereum"),
                )
            else:
                log.warning("OPENSEA_API_KEY missing — skipping OpenSea sales")

        if "magiceden" in self.markets:
            me_key = os.getenv("MAGICEDEN_API_KEY", "")
            log.warning("MagicEden EVM support deprecated (March 2025 shutdown): "
                        "EVM API calls will return 0 results. Consider using OpenSea or other alternatives.")
            self.clients["magiceden"] = MagicEdenSalesClient(
                me_key, chain=os.getenv("MAGICEDEN_CHAIN", "bsc"),
            )

        # Telegram
        self.alerter = TelegramAlerter(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        )

        # Fat Finger Sniper (v2)
        self.fat_finger = None
        try:
            from okx_nft_bot.sniper.fat_finger import FatFingerSniper
            self.fat_finger = FatFingerSniper()
        except ImportError:
            log.debug("FatFingerSniper not available")

        # Instant Buyer for Binance WL collections
        self.instant_buyer = None
        try:
            from okx_nft_bot.sniper.buyer import OKXInstantBuyer
            self.instant_buyer = OKXInstantBuyer()
        except ImportError:
            log.debug("OKXInstantBuyer not available")
        except Exception as exc:
            log.warning("OKXInstantBuyer init failed: %s", exc)

        # Offer Blaster for Binance WL collections
        self.offer_blaster = None
        try:
            from okx_nft_bot.sniper.offer_blaster import OfferBlaster
            self.offer_blaster = OfferBlaster()
        except ImportError:
            log.debug("OfferBlaster not available")
        except Exception as exc:
            log.warning("OfferBlaster init failed: %s", exc)

        # Rival Scanner — undercut rival offers on Binance WL
        self.counter_bidder = None
        if os.getenv("COUNTERBID_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                from okx_nft_bot.sniper.counter_bidder import CounterBidder
                self.counter_bidder = CounterBidder(self.binance_whitelist, self.buy_config)
            except ImportError:
                log.debug("CounterBidder not available")
            except Exception as exc:
                log.warning("CounterBidder init failed: %s", exc)

        okx_cols = [c for c in self.collections if c["market"] == "okx"]
        me_cols = [c for c in self.collections if c["market"] == "magiceden"]
        os_cols = [c for c in self.collections if c["market"] == "opensea"]
        okx_mode = "playwright-priapi" if (self._pw_stream and self._pw_stream.is_running) else "v5-per-collection"
        log.info(
            "Sales Stream Daemon init: markets=%s, interval=%ds, rivals=%d, "
            "collections: okx=%d, magiceden=%d, opensea=%d, okx_mode=%s, http=%s, "
            "binance_wl=%d",
            list(self.clients.keys()), self.interval,
            len(self.rival_wallets),
            len(okx_cols), len(me_cols), len(os_cols),
            okx_mode, HTTP_ENGINE, len(self.binance_whitelist),
        )

    @classmethod
    def from_settings(cls, settings) -> "SalesStreamDaemon":
        """Create daemon from bot Settings object (uses same .env config)."""
        return cls(
            db_path=str(settings.sales_db_path),
            interval=settings.sales_poll_interval,
            markets_csv=settings.sales_markets,
            registry_path=str(settings.registry_path),
        )

    def _load_registry(self, path: str) -> list[dict]:
        """Load enabled collections from registry JSON."""
        try:
            with open(path) as f:
                data = json.load(f)
            all_cols = data.get("collections", [])
            enabled = [c for c in all_cols if c.get("enabled", True)]
            log.info("Loaded %d/%d enabled collections from %s", len(enabled), len(all_cols), path)
            return enabled
        except FileNotFoundError:
            log.warning("Registry not found at %s — no collections to poll", path)
            return []
        except Exception as exc:
            log.error("Failed to load registry %s: %s", path, exc)
            return []

    def _load_rival_wallets(self) -> set[str]:
        raw = os.getenv("RIVAL_WALLETS", "")
        return {w.strip().lower() for w in raw.split(",") if w.strip()}

    def _load_binance_whitelist(self) -> dict[str, dict]:
        """Load Binance whitelisted collections from data/binance_whitelist.json.

        Returns dict keyed by lowercased contract_address → full entry dict.
        """
        wl_path = Path(os.getenv("BINANCE_WHITELIST_PATH", "./data/binance_whitelist.json"))
        if not wl_path.exists():
            log.info("Binance whitelist not found at %s — disabled", wl_path)
            return {}
        try:
            data = json.loads(wl_path.read_text())
            wl = {item["contract_address"].lower(): item for item in data if item.get("contract_address")}
            log.info("Binance whitelist loaded: %d collections", len(wl))
            return wl
        except Exception as exc:
            log.warning("Failed to load Binance whitelist: %s", exc)
            return {}

    def _load_buy_config(self) -> dict:
        """Load buy/offer price config from config/buy_config.json.

        Returns the full config dict with 'defaults', 'collections',
        'offer_settings', 'buy_settings'.
        """
        cfg_path = Path(os.getenv("BUY_CONFIG_PATH", "./config/buy_config.json"))
        if not cfg_path.exists():
            log.info("Buy config not found at %s — disabled", cfg_path)
            return {}
        try:
            cfg = json.loads(cfg_path.read_text())
            # Normalize collection keys to lowercase
            if "collections" in cfg:
                cfg["collections"] = {
                    k.lower(): v for k, v in cfg["collections"].items()
                }
            n_cols = len(cfg.get("collections", {}))
            buy_on = cfg.get("buy_settings", {}).get("enabled", False)
            offer_on = cfg.get("offer_settings", {}).get("enabled", False)
            log.info("Buy config loaded: %d collection overrides, buy=%s, offer=%s",
                     n_cols, buy_on, offer_on)
            return cfg
        except Exception as exc:
            log.warning("Failed to load buy config: %s", exc)
            return {}

    def get_buy_limits(self, collection_address: str, chain: str,
                       allow_defaults: bool = False) -> dict | None:
        """Get max buy/offer prices for a collection.

        Checks per-collection override first.
        Chain defaults are ONLY returned when allow_defaults=True
        (prevents garbage $0.54 offers on unknown collections).
        Returns None if buy config not loaded or collection disabled.
        Returns dict: {max_buy_price, max_offer_price, currency, name, ...}
        """
        if not self.buy_config:
            return None
        addr = collection_address.lower()
        # Per-collection override
        col_cfg = self.buy_config.get("collections", {}).get(addr)
        if col_cfg:
            if not col_cfg.get("enabled", True):
                return None
            # Skip auto-generated entries whose price equals the chain default
            # (0.0009 BNB / 0.000257 ETH = $0.54 junk entries)
            mp = col_cfg.get("max_offer_price") or col_cfg.get("max_buy_price", 0)
            cfg_cur = (col_cfg.get("currency") or "").upper()
            chain_key = (chain.lower(), cfg_cur)
            default_price = self._CHAIN_DEFAULTS.get(chain_key)
            if default_price is not None and mp and abs(mp - default_price) < 1e-9:
                # This is an auto-generated default entry, not a real config
                return None
            return col_cfg
        # Chain defaults — only if explicitly allowed (e.g. for WL buy triggers)
        if allow_defaults:
            defaults = self.buy_config.get("defaults", {}).get(chain.lower())
            if defaults:
                return defaults
        return None

    def _load_blacklist(self) -> set[str]:
        """Load collection address blacklist (junk to exclude).

        Set COLLECTION_BLACKLIST in .env as comma-separated addresses.
        Can also load from data/collection_blacklist.txt (one per line).
        """
        bl: set[str] = set()
        # From env
        raw = os.getenv("COLLECTION_BLACKLIST", "")
        bl.update(a.strip().lower() for a in raw.split(",") if a.strip())
        # From file
        bl_file = Path(os.getenv("BLACKLIST_FILE", "./data/collection_blacklist.txt"))
        if bl_file.exists():
            for line in bl_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    bl.add(line.lower())
        if bl:
            log.info("Collection blacklist: %d addresses", len(bl))
        return bl

    def poll_okx(self) -> list[SaleEvent]:
        """Poll OKX for NFT sales.

        Primary: Playwright headless browser intercepts priapi global feed
        (ALL sales across all collections, no per-collection limits).

        Fallback: v5 per-collection API when Playwright is unavailable.
        """
        client: OKXSalesClient = self.clients["okx"]

        # ── PRIMARY: Playwright priapi global stream ──────────────
        if self._pw_stream and self._pw_stream.is_running:
            raw_trades = self._pw_stream.drain_trades()
            if raw_trades:
                events = [client.normalize(t, self.rival_wallets) for t in raw_trades]
                # Filter out blacklisted collections
                if self.collection_blacklist:
                    before = len(events)
                    events = [e for e in events
                              if e.collection_address not in self.collection_blacklist]
                    filtered = before - len(events)
                    if filtered:
                        log.debug("Blacklist filtered %d/%d trades", filtered, before)
                # Stats
                collections_seen = {e.collection_address for e in events}
                chains_seen = {e.chain for e in events}
                log.info("OKX priapi (Playwright): %d trades, %d collections, chains=%s",
                         len(events), len(collections_seen), sorted(chains_seen))
                return events
            else:
                log.debug("OKX priapi (Playwright): buffer empty this cycle")
                return []

        # ── FALLBACK: v5 per-collection API ───────────────────────
        if not (client.api_key and client.api_secret and client.passphrase):
            log.warning("OKX: API keys missing and Playwright not active — cannot poll")
            return []

        all_events: list[SaleEvent] = []
        okx_collections = [c for c in self.collections if c.get("market") == "okx"]
        if not okx_collections:
            log.info("OKX: no OKX collections in registry — nothing to poll")
            return []

        for i, col in enumerate(okx_collections):
            addr = col.get("collection_address", "")
            if not addr:
                continue
            # Rate-limit: 1 req/sec between collections to avoid 429
            if i > 0:
                time.sleep(1.0)

            cursor_key = f"okx:{addr}"
            cursor = self.db.get_cursor(cursor_key)

            for attempt in range(3):
                trades, next_cursor = client.fetch_recent_trades(
                    collection_address=addr, cursor=cursor, limit=50,
                )
                if trades is not None:
                    break
                # Retry on rate limit with exponential backoff
                wait = 2 ** (attempt + 1)
                log.info("OKX rate-limited for %s, retrying in %ds...", addr[:10], wait)
                time.sleep(wait)

            events = [client.normalize(t, self.rival_wallets) for t in trades]
            all_events.extend(events)
            if next_cursor:
                self.db.set_cursor(cursor_key, next_cursor)
            if events:
                log.info("OKX v5 %s (%s): %d trades", col.get("name", "?"), addr[:10], len(events))
        return all_events

    def poll_opensea(self) -> list[SaleEvent]:
        client: OpenSeaSalesClient = self.clients["opensea"]
        # OpenSea events API can work globally — poll once with cursor
        cursor = self.db.get_cursor("opensea")
        raw_events, next_cursor = client.fetch_recent_sales(after=cursor, limit=50)
        events = [client.normalize(e, self.rival_wallets) for e in raw_events]
        if next_cursor:
            self.db.set_cursor("opensea", next_cursor)
        return events

    def poll_magiceden(self) -> list[SaleEvent]:
        client: MagicEdenSalesClient = self.clients["magiceden"]
        all_events: list[SaleEvent] = []
        me_collections = [c for c in self.collections if c.get("market") == "magiceden"]
        for col in me_collections:
            addr = col.get("collection_address", "")
            if not addr:
                continue
            cursor_key = f"magiceden:{addr}"
            cursor = self.db.get_cursor(cursor_key)
            sales, next_cont = client.fetch_recent_sales(
                contract=addr, continuation=cursor, limit=50,
            )
            events = [client.normalize(s, self.rival_wallets) for s in sales]
            all_events.extend(events)
            if next_cont:
                self.db.set_cursor(cursor_key, next_cont)
            if events:
                log.info("MagicEden %s (%s): %d sales", col.get("name", "?"), addr[:10], len(events))
        return all_events

    def run_once(self) -> dict:
        """Single poll pass across all markets.

        Returns {
            'by_market': {market: new_count},
            'by_chain': {chain: count},
            'rival_count': int,
            'total': int,
        }
        """
        by_market: dict[str, int] = {}
        by_chain: dict[str, int] = {}
        rival_count = 0

        pollers = {
            "okx": self.poll_okx,
            "opensea": self.poll_opensea,
            "magiceden": self.poll_magiceden,
        }

        for market, poller in pollers.items():
            if market not in self.clients:
                continue
            try:
                events = poller()

                # Apply stream filters (hot-reloadable)
                if self.filters:
                    self.filters.reload()  # check for config changes
                    events = self.filters.filter_events(events)

                new_count = self.db.save_events(events)
                by_market[market] = new_count

                # Count by chain
                for e in events:
                    by_chain[e.chain] = by_chain.get(e.chain, 0) + 1

                # Alert on rival activity
                for e in events:
                    if e.is_rival_seller or e.is_rival_buyer:
                        rival_count += 1
                        self.alerter.alert_rival_sale(e)
                        log.info(
                            "🚨 RIVAL %s on %s [%s]: %s #%s @ %.4f %s",
                            "SELL" if e.is_rival_seller else "BUY",
                            market.upper(), e.chain.upper(),
                            e.collection_name,
                            e.token_id, e.price, e.currency,
                        )
                        # Trigger rival scanner on this collection
                        if self.counter_bidder and self.binance_whitelist.get(e.collection_address.lower()):
                            try:
                                self.counter_bidder.hunt_collection(
                                    e.collection_address, e.collection_name, e.chain)
                            except Exception as hunt_exc:
                                log.error("Rival hunt failed for %s: %s",
                                          e.collection_name, hunt_exc)

                # Alert on Binance-whitelisted collection trades + auto-buy
                _wl_miss_log = 0
                if self.binance_whitelist:
                    for e in events:
                        addr_lower = e.collection_address.lower()
                        wl_info = self.binance_whitelist.get(addr_lower)
                        if not wl_info:
                            _wl_miss_log += 1
                            if _wl_miss_log <= 5:
                                log.debug("WL miss: %s %s [%s] @ %.4f %s",
                                          addr_lower[:14], e.collection_name[:30], e.chain, e.price, e.currency)
                        if wl_info:
                            buy_limits = self.get_buy_limits(e.collection_address, e.chain)
                            self.alerter.alert_binance_trade(e, wl_info, buy_limits)
                            log.info(
                                "💎 BINANCE WL on %s [%s]: %s #%s @ %.4f %s",
                                market.upper(), e.chain.upper(),
                                e.collection_name,
                                e.token_id, e.price, e.currency,
                            )

                            # AUTO-BUY: if price within buy range, try to grab cheapest listing
                            if buy_limits and self.instant_buyer:
                                max_buy = buy_limits.get("max_buy_price", 0)
                                if max_buy and e.price <= max_buy * 2:
                                    # Trade is near buy zone — scan for cheap listings NOW
                                    try:
                                        result = self.instant_buyer.try_buy(
                                            collection_address=e.collection_address,
                                            collection_name=e.collection_name,
                                            chain=e.chain,
                                            max_price=max_buy,
                                            currency=e.currency,
                                        )
                                        if result and result.success:
                                            log.warning(
                                                "🚀 AUTO-BUY SUCCESS: %s #%s @ %.6f %s tx=%s",
                                                e.collection_name, result.token_id,
                                                result.listing_price, e.currency,
                                                result.tx_hash or "?",
                                            )
                                    except Exception as buy_exc:
                                        log.error("Auto-buy failed for %s: %s",
                                                  e.collection_name, buy_exc)

                            # OFFER BLAST: undercut all offers on this collection
                            if buy_limits and self.offer_blaster:
                                max_offer = buy_limits.get("max_offer_price", 0)
                                if max_offer and self.offer_blaster.should_blast(e.collection_address):
                                    try:
                                        self.offer_blaster.blast_collection(
                                            collection_address=e.collection_address,
                                            collection_name=e.collection_name,
                                            chain=e.chain,
                                            max_offer_price=max_offer,
                                            currency=e.currency,
                                        )
                                    except Exception as blast_exc:
                                        log.error("Offer blast failed for %s: %s",
                                                  e.collection_name, blast_exc)

                # Fat Finger detection on this batch
                if self.fat_finger and events:
                    try:
                        ff_hits = self.fat_finger.process_sales_batch(events)
                        if ff_hits:
                            log.info("🎯 Fat finger hits: %d in %s", len(ff_hits), market)
                    except Exception as ff_exc:
                        log.debug("Fat finger check error: %s", ff_exc)

                if new_count > 0:
                    log.info("%s: +%d new sales", market.upper(), new_count)

            except Exception as exc:
                log.error("%s poll failed: %s", market.upper(), exc)
                by_market[market] = -1

        total = sum(v for v in by_market.values() if v > 0)
        return {
            "by_market": by_market,
            "by_chain": by_chain,
            "rival_count": rival_count,
            "total": total,
        }

    def run_daemon(self, max_cycles: int = 0):
        """Run continuously. max_cycles=0 means forever."""
        cycle = 0
        summary_interval = int(os.getenv("TELEGRAM_SUMMARY_INTERVAL", "20"))  # send summary every N cycles
        cumulative_trades = 0
        cumulative_rivals = 0
        cumulative_by_chain: dict[str, int] = {}

        log.info("Starting Sales Stream Daemon (interval=%ds, max_cycles=%s, summary_every=%d cycles)",
                 self.interval, max_cycles or "∞", summary_interval)

        # Send startup message to Telegram
        self.alerter.send_summary(0, 0, 0, 0, {})

        # Start CounterBidder background scanner
        if self.counter_bidder:
            self.counter_bidder.start_background_scan()

        while True:
            cycle += 1
            try:
                results = self.run_once()
                total_new = results["total"]
                cumulative_trades += total_new
                cumulative_rivals += results["rival_count"]
                for chain, cnt in results["by_chain"].items():
                    cumulative_by_chain[chain] = cumulative_by_chain.get(chain, 0) + cnt

                if total_new > 0:
                    log.info("Cycle %d: %d new sales %s chains=%s",
                             cycle, total_new, results["by_market"], results["by_chain"])

                # Send periodic summary to Telegram
                if cycle % summary_interval == 0:
                    self.alerter.send_summary(
                        cycle, cumulative_trades, cumulative_rivals,
                        len(cumulative_by_chain), cumulative_by_chain,
                    )
                    # Reset counters
                    cumulative_trades = 0
                    cumulative_rivals = 0
                    cumulative_by_chain = {}

            except Exception as exc:
                log.error("Cycle %d failed: %s", cycle, exc)

            if max_cycles and cycle >= max_cycles:
                break

            time.sleep(self.interval)

        # Graceful shutdown of Playwright stream
        if self._pw_stream:
            try:
                self._pw_stream.stop()
            except Exception as exc:
                log.warning("Playwright stream shutdown failed: %s", exc)

        stats = self.db.get_stats()
        log.info("Daemon stopped after %d cycles. Stats: %s", cycle, stats)
        return stats


# ─── CLI ────────────────────────────────────────────────────────

def main():
    import argparse

    # Load .env if present
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    parser = argparse.ArgumentParser(
        prog="sales-stream",
        description="Real-time NFT sales stream daemon for OKX, OpenSea, MagicEden",
    )
    parser.add_argument("--once", action="store_true", help="Run single poll pass")
    parser.add_argument("--interval", type=int, default=None, help="Poll interval in seconds")
    parser.add_argument("--markets", type=str, default=None, help="Comma-separated markets")
    parser.add_argument("--stats", action="store_true", help="Show DB stats and exit")
    args = parser.parse_args()

    if args.interval:
        os.environ["SALES_POLL_INTERVAL"] = str(args.interval)
    if args.markets:
        os.environ["SALES_MARKETS"] = args.markets

    daemon = SalesStreamDaemon()

    if args.stats:
        print(json.dumps(daemon.db.get_stats(), indent=2, ensure_ascii=False))
        return 0

    if args.once:
        results = daemon.run_once()
        print(json.dumps(results, indent=2))
        return 0

    daemon.run_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())

