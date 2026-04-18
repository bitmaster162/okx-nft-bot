#!/usr/bin/env python3
"""Scan BSC WL collections for active offers and generate buy_config entries.

For each WL collection with active offers:
- Shows current best offer price
- Suggests max_price based on floor / offer data
- Outputs JSON ready to paste into buy_config.json

Usage:
  docker exec okx-nft-bot-sales-stream python3 /app/scripts/gen_price_config.py
"""
import json, sys, os, time
sys.path.insert(0, "/app/src")

from pathlib import Path

# ── Load WL ──────────────────────────────────────────────────
wl_path = Path("./data/binance_whitelist.json")
wl = json.loads(wl_path.read_text(encoding="utf-8"))

bsc_wl = [
    c for c in wl
    if c.get("network", "").lower() == "bsc"
    and c.get("okx_verified", False)
]
print(f"BSC WL (okx_verified): {len(bsc_wl)} collections\n")

# ── Init OKX client ──────────────────────────────────────────
from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

settings = load_settings()
client = OKXAPIClient(settings=settings)

# ── Scan each collection ─────────────────────────────────────
results = []

for i, coll in enumerate(bsc_wl):
    addr = coll["contract_address"].lower()
    name = coll.get("collection_name", addr[:14])

    try:
        payload = client._request(
            method="GET",
            path="/api/v5/mktplace/nft/markets/collection-offers",
            params={
                "chain": "bsc",
                "collectionAddress": addr,
                "status": "active",
                "limit": 5,
            },
        )
        data = payload.get("data", {})
        items = data.get("data", []) if isinstance(data, dict) else []
    except Exception as exc:
        print(f"  [{i+1}/{len(bsc_wl)}] {name}: ERROR {exc}")
        continue

    if not items:
        continue

    # Parse offers
    offers = []
    for item in items:
        price_raw = item.get("price", "0")
        cur_addr = item.get("currencyAddress", "")
        maker = (item.get("maker") or "").lower()

        # Determine currency from address
        CURRENCIES = {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18),
            "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18),
            "0xe9e7cea3dedca5984780bafc599bd69add087d56": ("BUSD", 18),
        }
        cur_info = CURRENCIES.get(cur_addr.lower(), ("UNK", 18))
        cur_name, decimals = cur_info
        price = int(price_raw) / (10 ** decimals) if price_raw.isdigit() else 0

        offers.append({
            "price": price,
            "currency": cur_name,
            "maker": maker,
        })

    if not offers:
        continue

    best = max(offers, key=lambda o: o["price"])
    cheapest = min(offers, key=lambda o: o["price"])

    results.append({
        "address": addr,
        "name": name,
        "best_price": best["price"],
        "best_currency": best["currency"],
        "cheapest_price": cheapest["price"],
        "cheapest_currency": cheapest["currency"],
        "num_offers": len(offers),
    })

    print(f"  [{i+1}/{len(bsc_wl)}] {name}: {len(offers)} offers, "
          f"best={best['price']:.6f} {best['currency']}, "
          f"cheapest={cheapest['price']:.6f} {cheapest['currency']}")

    # Rate limit
    if i % 20 == 19:
        time.sleep(1.0)
    else:
        time.sleep(0.15)

# ── Sort by best price desc ──────────────────────────────────
results.sort(key=lambda r: r["best_price"], reverse=True)

print(f"\n{'='*70}")
print(f"FOUND {len(results)} BSC WL collections with active offers\n")

# ── Print table ──────────────────────────────────────────────
print(f"{'Collection':<30} {'Best Price':>14} {'Cur':>6} {'#Offers':>8}")
print("-" * 65)
for r in results:
    print(f"{r['name'][:30]:<30} {r['best_price']:>14.6f} {r['best_currency']:>6} {r['num_offers']:>8}")

# ── Generate config entries ──────────────────────────────────
# For collections with offers > $0.41 equivalent, suggest max_price
BNB_USD = 615  # approximate
USDT_USD = 1
GLOBAL_MAX = 0.41

config_entries = {}
for r in results:
    cur = r["best_currency"]
    price = r["best_price"]

    # Convert to USD
    if cur in ("WBNB", "BNB"):
        usd = price * BNB_USD
    elif cur in ("USDT", "USDC", "BUSD"):
        usd = price
    else:
        usd = 0

    if usd <= GLOBAL_MAX:
        continue  # Global cap already covers this

    # Suggest max_price = best_offer * 1.05 (5% above to ensure we win)
    # but user will need to review and adjust
    suggested = round(price * 1.05, 6)

    config_entries[r["address"]] = {
        "name": r["name"],
        "max_offer_price": suggested,
        "max_buy_price": suggested,
        "currency": cur,
        "enabled": True,
        "_current_best": f"{price:.6f} {cur} (~${usd:.2f})",
    }

print(f"\n{'='*70}")
print(f"Collections with offers ABOVE ${GLOBAL_MAX} (need custom max_price):\n")
for addr, cfg in config_entries.items():
    print(f"  {cfg['name']}: {cfg['_current_best']} → suggested max={cfg['max_offer_price']:.6f} {cfg['currency']}")

# ── Save suggested config ────────────────────────────────────
out_path = Path("./data/suggested_price_config.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(config_entries, f, ensure_ascii=False, indent=2)
    f.flush()
    os.fsync(f.fileno())

print(f"\nSuggested config saved to {out_path}")
print("Review and copy entries into config/buy_config.json → collections: {}")
