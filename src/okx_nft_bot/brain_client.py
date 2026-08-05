"""Brain HTTP client — bots talk to Anti-Parasite Brain via this module.

Lazy / fail-soft: if Brain is unreachable, all calls return None silently.
Bots should NOT block on Brain failures — use it as best-effort intel layer.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


BRAIN_URL = os.environ.get("BRAIN_URL", "http://172.17.0.1:9100")
BRAIN_TOKEN = os.environ.get("BRAIN_TOKEN", "")
BRAIN_TIMEOUT = float(os.environ.get("BRAIN_TIMEOUT", "5"))


def _request(method: str, path: str, body: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Internal helper. Returns parsed JSON or None on any failure."""
    if not BRAIN_TOKEN:
        return None
    url = f"{BRAIN_URL}{path}"
    headers = {"X-Brain-Token": BRAIN_TOKEN, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=BRAIN_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None
    except Exception:
        return None


# ── Parasite tracking ──

def report_parasite_sighting(
    *,
    wallet: str,
    platform: str = "okx",
    chain: Optional[str] = None,
    collection: Optional[str] = None,
    price_usd: Optional[float] = None,
    notes: Optional[str] = None,
    bot_id: str = "okx_nft_bot_v13",
) -> Optional[dict[str, Any]]:
    """POST /parasite/seen — report we saw a parasite. Brain auto-promotes after 3 sightings."""
    return _request("POST", "/parasite/seen", {
        "wallet": wallet,
        "platform": platform,
        "chain": chain,
        "collection": collection,
        "price_usd": price_usd,
        "notes": notes,
        "reporter_bot_id": bot_id,
    })


def get_confirmed_parasites() -> list[str]:
    """GET /parasites — return list of confirmed parasite wallet addresses (lower-case)."""
    resp = _request("GET", "/parasites?min_status=confirmed")
    if not resp:
        return []
    return [w["wallet"].lower() for w in resp.get("wallets", [])]


# ── Alerts ──

_alert_dedupe: dict[str, float] = {}  # in-process dedupe to avoid spam Brain


def send_alert(
    *,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    text: Optional[str] = None,
    bot_id: str = "okx_nft_bot_v13",
    local_dedupe_seconds: int = 300,
) -> Optional[dict[str, Any]]:
    """POST /alert — send alert to Brain (which dedupes + sends to TG).

    local_dedupe_seconds: in-process dedupe to avoid POSTing same alert in burst.
    """
    cache_key = f"{kind}:{dedupe_key}"
    now = time.time()
    last = _alert_dedupe.get(cache_key, 0)
    if now - last < local_dedupe_seconds:
        return None
    _alert_dedupe[cache_key] = now
    return _request("POST", "/alert", {
        "kind": kind,
        "dedupe_key": dedupe_key,
        "payload": payload,
        "text": text,
        "reporter_bot_id": bot_id,
    })


# ── Heartbeats ──

def heartbeat(
    *,
    bot_id: str = "okx_nft_bot_v13",
    wallet: Optional[str] = None,
    status: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """POST /bot/heartbeat — periodic liveness signal. Call every 60s."""
    return _request("POST", "/bot/heartbeat", {
        "bot_id": bot_id,
        "wallet": wallet,
        "status": status or {},
    })


# ── Health check ──

def health() -> Optional[dict[str, Any]]:
    """GET /health — confirm Brain reachable."""
    return _request("GET", "/health")
