#!/usr/bin/env python3
"""Test if chain=bsc vs chain=BSC makes difference for maker filter."""
import json, time, os
from pathlib import Path

log_path = Path("./data/test_bsc_maker.log")
fh = open(str(log_path), "w", buffering=1)

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line)
    fh.write(line + "\n")

from okx_nft_bot.config import load_settings
from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.clients.http import StdlibHttpTransport, build_url

settings = load_settings()
client = OKXMarketplaceClient(settings=settings)

PARASITE = "0xf1771cf8831393422189330a79dd896223c357a4"
# Known collection with offers (DPGU)
DPGU = "0xdf7952b35f24acf7fc0487d01c8d5690a60dba07"

tests = [
    # Test 1: chain=bsc, collectionAddress (should work)
    {"chain": "bsc", "collectionAddress": DPGU, "status": "active", "limit": 5},
    # Test 2: chain=BSC, collectionAddress
    {"chain": "BSC", "collectionAddress": DPGU, "status": "active", "limit": 5},
    # Test 3: chain=bsc, maker=parasite
    {"chain": "bsc", "maker": PARASITE, "status": "active", "limit": 10},
    # Test 4: chain=BSC, maker=parasite
    {"chain": "BSC", "maker": PARASITE, "status": "active", "limit": 10},
    # Test 5: chain=bsc, maker=parasite, collectionAddress=DPGU
    {"chain": "bsc", "maker": PARASITE, "collectionAddress": DPGU, "status": "active", "limit": 5},
    # Test 6: chain=BSC, maker=parasite, collectionAddress=DPGU
    {"chain": "BSC", "maker": PARASITE, "collectionAddress": DPGU, "status": "active", "limit": 5},
]

for i, params in enumerate(tests):
    path = "/api/v5/mktplace/nft/markets/offers"
    log(f"\n--- TEST {i+1}: {path}")
    log(f"    params: {params}")

    try:
        url, request_path = build_url(settings.okx_api_base, path, params)
        headers = client._build_headers(method="GET", request_path=request_path, body="")
        payload = client.transport.request_json(method="GET", url=url, headers=headers, body="")

        data = payload.get("data")
        if isinstance(data, dict):
            items = data.get("data", [])
            cursor = data.get("cursor", "")
        elif isinstance(data, list):
            items = data
            cursor = ""
        else:
            items = []
            cursor = ""

        log(f"    items: {len(items)}, cursor: {bool(cursor)}")
        if items:
            for item in items[:3]:
                maker = item.get("makerAddress") or item.get("maker") or "?"
                price = item.get("price") or item.get("offerPrice") or "?"
                currency = item.get("currencyName") or item.get("currency") or "?"
                coll = item.get("collectionAddress") or "?"
                log(f"    -> maker={maker[:14]}... price={price} {currency} coll={coll[:14]}...")
        else:
            # Show raw response
            log(f"    raw code: {payload.get('code')}, msg: {payload.get('msg', '')[:100]}")
            log(f"    raw data type: {type(data).__name__}, keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
    except Exception as e:
        log(f"    ERROR: {e}")

    time.sleep(0.5)

# Also test collection-offers endpoint
for chain_val in ["bsc", "BSC"]:
    path = "/api/v5/mktplace/nft/markets/collection-offers"
    params = {"chain": chain_val, "maker": PARASITE, "status": "active", "limit": 10}
    log(f"\n--- COLLECTION-OFFERS: chain={chain_val}, maker={PARASITE[:14]}...")
    try:
        url, request_path = build_url(settings.okx_api_base, path, params)
        headers = client._build_headers(method="GET", request_path=request_path, body="")
        payload = client.transport.request_json(method="GET", url=url, headers=headers, body="")
        data = payload.get("data")
        items = []
        if isinstance(data, dict):
            items = data.get("data", [])
        elif isinstance(data, list):
            items = data
        log(f"    items: {len(items)}")
        if items:
            for item in items[:3]:
                maker = item.get("makerAddress") or item.get("maker") or "?"
                price = item.get("price") or item.get("offerPrice") or "?"
                coll = item.get("collectionAddress") or "?"
                log(f"    -> maker={maker[:14]}... price={price} coll={coll[:14]}...")
    except Exception as e:
        log(f"    ERROR: {e}")
    time.sleep(0.5)

log("\nDONE")
