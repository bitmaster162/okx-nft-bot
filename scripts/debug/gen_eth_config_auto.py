#!/usr/bin/env python3
"""Scan ETH WL collections, find max offer, set max_price = max_offer + 1 step.

Outputs JSON ready to merge into buy_config.json.

Usage:
  docker exec okx-nft-bot-sales-stream python3 /app/scripts/gen_eth_config_auto.py
"""
import json, sys, os, time
sys.path.insert(0, "/app/src")
from pathlib import Path

wl_path = Path("./data/binance_whitelist.json")
wl = json.loads(wl_path.read_text(encoding="utf-8"))

eth_wl = [c for c in wl if c.get("network", "").lower() == "eth"]
print(f"ETH WL: {len(eth_wl)} collections\n")

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

settings = load_settings()
client = OKXAPIClient(settings=settings)

CURRENCIES = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
}

# Step sizes for each currency
STEP = {
    "WETH": 0.0001,   # ~$0.21
    "USDT": 0.01,
    "USDC": 0.01,
    "DAI":  0.01,
}

results = []

for i, coll in enumerate(eth_wl):
    addr = coll["contract_address"].lower()
    name = coll.get("collection_name", addr[:14])

    all_items = []
    for path in ["/api/v5/mktplace/nft/markets/offers",
                 "/api/v5/mktplace/nft/markets/collection-offers"]:
        try:
            payload = client._request(
                method="GET",
                path=path,
                params={
                    "chain": "eth",
                    "collectionAddress": addr,
                    "status": "active",
                    "limit": 5,
                },
            )
            data = payload.get("data", {})
            items = data.get("data", []) if isinstance(data, dict) else []
            all_items.extend(items)
        except Exception:
            pass

    if not all_items:
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(eth_wl)}] scanning...")
        if i % 20 == 19:
            time.sleep(1.0)
        else:
            time.sleep(0.15)
        continue

    offers = []
    seen = set()
    for item in all_items:
        price_raw = item.get("price", "0")
        cur_addr = (item.get("currencyAddress") or "").lower()
        order_hash = item.get("orderHash", "")
        if order_hash in seen:
            continue
        seen.add(order_hash)

        cur_info = CURRENCIES.get(cur_addr, ("UNK", 18))
        cur_name, decimals = cur_info
        try:
            price = int(price_raw) / (10 ** decimals) if price_raw.isdigit() else 0
        except:
            price = 0

        if price > 0 and cur_name != "UNK":
            offers.append({"price": price, "currency": cur_name})

    if not offers:
        if i % 20 == 19:
            time.sleep(1.0)
        else:
            time.sleep(0.15)
        continue

    best = max(offers, key=lambda o: o["price"])
    step = STEP.get(best["currency"], 0.01)
    max_price = round(best["price"] + step, 6)

    # Convert to USD for display
    if best["currency"] == "WETH":
        usd = best["price"] * 2100
    elif best["currency"] in ("USDT", "USDC", "DAI"):
        usd = best["price"]
    else:
        usd = 0

    results.append({
        "address": addr,
        "name": name,
        "best_price": best["price"],
        "max_price": max_price,
        "currency": best["currency"],
        "best_usd": usd,
        "num_offers": len(offers),
    })

    print(f"  [{i+1}/{len(eth_wl)}] {name[:35]}: best={best['price']:.6f} {best['currency']} -> max={max_price:.6f} (~${usd:.2f})")

    if i % 20 == 19:
        time.sleep(1.0)
    else:
        time.sleep(0.15)

results.sort(key=lambda r: r["best_usd"], reverse=True)

print(f"\n{'='*80}")
print(f"FOUND {len(results)} ETH WL collections with active offers\n")

print(f"{'Collection':<35} {'Best':>12} {'Max':>12} {'Cur':>6} {'~USD':>8}")
print("-" * 80)
for r in results:
    print(f"{r['name'][:35]:<35} {r['best_price']:>12.6f} {r['max_price']:>12.6f} {r['currency']:>6} {r['best_usd']:>8.2f}")

# Generate config entries
config_entries = {}
for r in results:
    config_entries[r["address"]] = {
        "name": r["name"],
        "max_offer_price": r["max_price"],
        "max_buy_price": r["max_price"],
        "currency": r["currency"],
        "enabled": True,
    }

# Write to temp file for merging
out_path = Path("./config/eth_auto_config.json")
out_path.write_text(json.dumps(config_entries, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nConfig entries saved to {out_path} ({len(config_entries)} collections)")
