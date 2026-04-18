#!/usr/bin/env python3
"""Debug: dump raw /collection-offers response structure for parasite wallet."""
import json, sys, os
sys.path.insert(0, "/app/src")

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

settings = load_settings()
client = OKXAPIClient(settings=settings)

WALLET = "0xf1771cf8831393422189330a79dd896223c357a4"

print("=== RAW /collection-offers response ===")
payload = client._request(
    method="GET",
    path="/api/v5/mktplace/nft/markets/collection-offers",
    params={
        "chain": "bsc",
        "maker": WALLET,
        "status": "active",
        "limit": 10,
    },
)

print(f"Top-level type: {type(payload).__name__}")
print(f"Top-level keys: {sorted(payload.keys())}")

data = payload.get("data")
print(f"\ndata type: {type(data).__name__}")
if isinstance(data, dict):
    print(f"data keys: {sorted(data.keys())}")
    for k, v in data.items():
        if isinstance(v, list):
            print(f"  data['{k}']: list of {len(v)} items")
            if v:
                print(f"    [0] type: {type(v[0]).__name__}")
                if isinstance(v[0], dict):
                    print(f"    [0] keys: {sorted(v[0].keys())}")
                    # Print first item truncated
                    print(f"    [0]: {json.dumps(v[0], ensure_ascii=False, default=str)[:600]}")
        elif isinstance(v, str):
            print(f"  data['{k}']: '{v[:100]}'")
        else:
            print(f"  data['{k}']: {type(v).__name__} = {v}")
elif isinstance(data, list):
    print(f"data: list of {len(data)} items")
    if data:
        print(f"  [0] type: {type(data[0]).__name__}")
        if isinstance(data[0], dict):
            print(f"  [0] keys: {sorted(data[0].keys())}")
            print(f"  [0]: {json.dumps(data[0], ensure_ascii=False, default=str)[:600]}")

print("\n=== FULL JSON (first 3000 chars) ===")
print(json.dumps(payload, ensure_ascii=False, default=str)[:3000])
