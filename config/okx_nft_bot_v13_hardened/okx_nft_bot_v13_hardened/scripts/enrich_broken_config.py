#!/usr/bin/env python3
"""Enrich buy_config entries that have hex-stub names and empty currency.

Uses curl_cffi (same as the bot) to bypass OKX anti-bot protection.

Usage:
  docker exec okx-nft-bot-sales-stream python3 /app/data/enrich_broken_config.py [--dry-run] [--apply]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as http
except ImportError:
    print("ERROR: curl_cffi not installed. Run inside Docker container.")
    sys.exit(1)

CONFIG_PATH = Path("/app/config/buy_config.json") if Path("/app").exists() else Path("config/buy_config.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

CURRENCY_NAMES = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
}


def _get(url: str) -> dict:
    resp = http.get(url, headers=HEADERS, timeout=10, impersonate="chrome")
    return resp.json()


def try_collection_offers(addr: str, chain_id: str) -> dict | None:
    """Method 1: collection-offers — same endpoint that found parasites."""
    try:
        data = _get(
            f"https://web3.okx.com/priapi/v5/nft/ec/collection-offer/list"
            f"?collectionAddress={addr}&chainId={chain_id}&limit=1&status=active"
        )
        items = data.get("data", {})
        if isinstance(items, dict):
            items = items.get("offers", items.get("data", []))
        if not items:
            return None
        item = items[0]
        name = item.get("collectionName") or item.get("name") or ""
        cur_addr = (item.get("currencyAddress") or "").lower()
        currency = CURRENCY_NAMES.get(cur_addr, "")
        return {"name": name, "currency": currency}
    except Exception:
        return None


def try_offers(addr: str, chain_id: str) -> dict | None:
    """Method 2: token-level offers."""
    try:
        data = _get(
            f"https://web3.okx.com/priapi/v5/nft/ec/offer/list"
            f"?collectionAddress={addr}&chainId={chain_id}&limit=1&status=active"
        )
        items = data.get("data", {})
        if isinstance(items, dict):
            items = items.get("offers", items.get("data", []))
        if not items:
            return None
        item = items[0]
        name = item.get("collectionName") or item.get("name") or ""
        cur_addr = (item.get("currencyAddress") or "").lower()
        currency = CURRENCY_NAMES.get(cur_addr, "")
        return {"name": name, "currency": currency} if name or currency else None
    except Exception:
        return None


def try_asset_list(addr: str, chain_id: str) -> dict | None:
    """Method 3: asset-list."""
    try:
        data = _get(
            f"https://web3.okx.com/priapi/v5/nft/ec/asset/list"
            f"?chainId={chain_id}&contractAddress={addr}&limit=1"
        )
        items = data.get("data", {})
        if isinstance(items, dict):
            items = items.get("data", [])
        if not items:
            return None
        asset = items[0]
        coll = asset.get("collection", {}) or {}
        name = coll.get("name", "") or asset.get("name", "")
        return {"name": name, "currency": ""} if name else None
    except Exception:
        return None


def resolve_collection(addr: str) -> dict | None:
    """Try all methods on both chains."""
    chain_map = {"56": ("bsc", "WBNB"), "1": ("eth", "WETH")}
    methods = [try_collection_offers, try_offers, try_asset_list]

    for chain_id in ("56", "1"):
        for method in methods:
            result = method(addr, chain_id)
            if result and (result.get("name") or result.get("currency")):
                chain_name, default_cur = chain_map[chain_id]
                name = result.get("name", "")
                currency = result.get("currency", "") or default_cur
                if name:
                    return {"name": name, "currency": currency, "chain": chain_name}
            time.sleep(0.08)

    return None


def main():
    apply = "--apply" in sys.argv

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    collections = cfg["collections"]

    broken = {}
    for addr, info in collections.items():
        cur = info.get("currency", "")
        name = info.get("name", "")
        if not cur or name.startswith("0x"):
            broken[addr] = info

    print(f"Found {len(broken)} broken entries to enrich")
    print(f"Using curl_cffi (impersonate=chrome)")
    print(f"Methods: collection-offers → offers → asset-list")
    print(f"Chains: BSC first, then ETH\n")

    fixed = 0
    not_found = 0

    for i, (addr, info) in enumerate(broken.items()):
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(broken)}] processed... (fixed={fixed}, not_found={not_found})")

        result = resolve_collection(addr)

        if result and result.get("name"):
            old_name = info.get("name", "")
            new_name = result["name"]
            currency = result["currency"]

            if apply:
                collections[addr]["name"] = new_name
                if not collections[addr].get("currency"):
                    collections[addr]["currency"] = currency

            print(f"  FIXED {addr[:14]}... {old_name} → {new_name} ({currency})")
            fixed += 1
        else:
            not_found += 1
            if not_found <= 20:
                print(f"  SKIP  {addr[:14]}... (not found)")

        time.sleep(0.15)

    print(f"\n{'='*60}")
    print(f"RESULTS: fixed={fixed}, not_found={not_found}")
    print(f"Total broken: {len(broken)}")

    if apply:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print(f"\nSaved to {CONFIG_PATH}")
    else:
        print(f"\nDRY RUN — use --apply to save changes")


if __name__ == "__main__":
    main()
