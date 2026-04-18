#!/usr/bin/env python3
"""Parse ALL BSC collections from OKX and cross-reference with Binance WL.

Run inside Docker:
  docker exec okx-nft-bot-sales-stream python3 /app/scripts/parse_bsc_collections.py
  docker exec okx-nft-bot-sales-stream python3 /app/scripts/parse_bsc_collections.py --update-wl
"""
import json
import time
import sys
import os
import builtins
from pathlib import Path

# Tee all output to data/parse_bsc.log
_log_file = open("./data/parse_bsc.log", "w", buffering=1)
_orig_print = builtins.print
def print(*args, **kwargs):
    _orig_print(*args, **kwargs)
    kwargs.pop("file", None)
    _orig_print(*args, file=_log_file, **kwargs)
builtins.print = print

# ── Load current WL ──────────────────────────────────────
wl_path = Path("./data/binance_whitelist.json")
wl_data = json.loads(wl_path.read_text()) if wl_path.exists() else []
wl_bsc = {item["contract_address"].lower(): item for item in wl_data
           if item.get("contract_address") and item.get("network", "").lower() == "bsc"}
print(f"WL loaded: {len(wl_data)} total, {len(wl_bsc)} BSC")

# ── HTTP client ──────────────────────────────────────────
from okx_nft_bot.config import load_settings
from okx_nft_bot.clients.okx import OKXMarketplaceClient
settings = load_settings()
client = OKXMarketplaceClient(settings=settings)

# ── Fetch ALL BSC collections from OKX ───────────────────
print("\n" + "="*60)
print("Fetching ALL BSC collections from OKX marketplace")
print("="*60)

okx_bsc = {}  # addr -> {name, slug, floor, volume}

cursor = ""
for page in range(100):  # up to 10k collections
    try:
        resp = client.get_collection_list(chain="bsc", limit=100, cursor=cursor or None)
        items = []
        if isinstance(resp, dict):
            d = resp.get("data", resp)
            if isinstance(d, dict):
                items = d.get("data", [])
                cursor = d.get("cursor", "")
            elif isinstance(d, list):
                items = d
                cursor = ""

        if not items:
            break

        for c in items:
            addr = ""
            for ac in (c.get("assetContracts") or []):
                a = (ac.get("contractAddress") or ac.get("address") or "").lower()
                if a:
                    addr = a
                    break
            if not addr:
                continue
            name = c.get("name") or c.get("collectionName") or c.get("slug") or addr[:14]
            slug = c.get("slug") or ""
            stats = c.get("stats") or {}
            okx_bsc[addr] = {
                "name": name,
                "slug": slug,
                "floor": stats.get("floorPrice", "0"),
                "volume": stats.get("totalVolume", "0"),
            }
        print(f"  Page {page+1}: {len(items)} items, total: {len(okx_bsc)}")
        if not cursor:
            break
        time.sleep(0.7)
    except Exception as e:
        print(f"  Error: {e}")
        break

print(f"\nTotal OKX BSC collections: {len(okx_bsc)}")

# ── Cross-reference ──────────────────────────────────────
print("\n" + "="*60)
print("Cross-referencing Binance WL with OKX")
print("="*60)

matched = {}      # WL entries found on OKX
not_on_okx = {}   # WL entries NOT on OKX

for addr, wl_info in wl_bsc.items():
    if addr in okx_bsc:
        matched[addr] = {**wl_info, "okx": okx_bsc[addr]}
    else:
        not_on_okx[addr] = wl_info

print(f"BSC WL total: {len(wl_bsc)}")
print(f"  On OKX: {len(matched)}")
print(f"  NOT on OKX: {len(not_on_okx)}")

if matched:
    print(f"\n  Matched collections:")
    for addr, info in sorted(matched.items(), key=lambda x: x[1].get("collection_name", "")):
        okx = info["okx"]
        print(f"    {info.get('collection_name', '')[:35]:<37} floor={okx['floor']:<12} vol={okx['volume'][:10]:<12} {addr[:14]}")

if not_on_okx:
    print(f"\n  NOT found on OKX (bot will get 404/empty for these):")
    for addr, info in sorted(not_on_okx.items(), key=lambda x: x[1].get("collection_name", "")):
        print(f"    {info.get('collection_name', '')[:40]:<42} {addr}")

# ── Save full OKX list ───────────────────────────────────
full_out = Path("./data/okx_bsc_full.json")
try:
    fh = open(str(full_out), "w", buffering=1)
    fh.write(json.dumps(okx_bsc, indent=2))
    fh.flush()
    os.fsync(fh.fileno())
    fh.close()
    print(f"\nSaved full OKX BSC list ({len(okx_bsc)}) to {full_out}")
except Exception as e:
    print(f"\nERROR writing {full_out}: {e}")

# ── Update WL with okx_verified flag ────────────────────
if "--update-wl" in sys.argv:
    print("\nUpdating WL with okx_verified flag...")
    updated = 0
    for item in wl_data:
        addr = (item.get("contract_address") or "").lower()
        if item.get("network", "").lower() == "bsc":
            if addr in okx_bsc:
                item["okx_verified"] = True
                item["okx_slug"] = okx_bsc[addr].get("slug", "")
                updated += 1
            else:
                item["okx_verified"] = False
    try:
        fh = open(str(wl_path), "w", buffering=1)
        fh.write(json.dumps(wl_data, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        print(f"WL updated: {updated} BSC collections tagged as okx_verified")
    except Exception as e:
        print(f"ERROR writing WL: {e}")

print("\nDONE")
