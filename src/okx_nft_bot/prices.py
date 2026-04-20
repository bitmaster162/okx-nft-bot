"""Shared lazy price helpers for execution-track engines.

Provides a tiny BNB→USD lookup with a 60s in-process cache, used by engines
that need to compute ``price_usd`` for ``ExecutionGovernor.check_min_price``.
Falls back to 0.0 on network failure so callers can decide how to react.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
_CACHE_TTL = 60.0
_STABLES = {"USDT", "USDC", "BUSD", "DAI", "TUSD"}

_lock = threading.Lock()
_cache: dict[str, float] = {}
_cache_ts: float = 0.0


def _refresh() -> None:
    global _cache, _cache_ts
    try:
        try:
            from curl_cffi import requests as http
        except ImportError:
            import requests as http  # type: ignore
        resp = http.get(_BINANCE_TICKER, timeout=5)
        data = resp.json()
        new_cache: dict[str, float] = {}
        for item in data:
            sym = item.get("symbol", "")
            if sym == "BNBUSDT":
                price = float(item["price"])
                new_cache["BNB"] = price
                new_cache["WBNB"] = price
            elif sym == "ETHUSDT":
                price = float(item["price"])
                new_cache["ETH"] = price
                new_cache["WETH"] = price
        if new_cache:
            _cache = new_cache
            _cache_ts = time.time()
    except Exception as exc:
        log.warning("prices: ticker fetch failed: %s", exc)


def get_usd_price(currency: str) -> float:
    cur = currency.upper().strip()
    if cur in _STABLES or cur == "USD":
        return 1.0
    with _lock:
        if time.time() - _cache_ts >= _CACHE_TTL:
            _refresh()
        return _cache.get(cur, 0.0)


def to_usd(amount: float, currency: str = "BNB") -> float:
    rate = get_usd_price(currency)
    return float(amount) * rate if rate > 0 else 0.0
