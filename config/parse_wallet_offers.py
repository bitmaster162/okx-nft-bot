#!/usr/bin/env python3
"""Parse all offers from a wallet and find collections missing from buy_config.json.

Usage:
  docker exec okx-nft-bot-sales-stream python3 /app/config/parse_wallet_offers.py
"""
import json, sys, time
sys.path.insert(0, "/app/src")
from pathlib import Path

TARGET_WALLET = "0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7"

# Load existing buy_config
config_path = Path("./config/buy_config.json")
config = json.loads(config_path.read_text(encoding="utf-8"))
existing_addrs = set(k.lower() for k in config.get("collections", {}).keys())
print(f"Existing collections in buy_config: {len(existing_addrs)}\n")

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

settings = load_settings()
client = OKXAPIClient(settings=settings)

BSC_CURRENCIES = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18),
    "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": ("BUSD", 18),
}

ETH_CURRENCIES = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
}

all_offers = []

for chain, currencies in [("bsc", BSC_CURRENCIES), ("eth", ETH_CURRENCIES)]:
    print(f"=== Scanning {chain.upper()} offers for {TARGET_WALLET[:14]}... ===")

    # collection-offers endpoint (works for both chains)
    cursor = None
    page = 0
    for _ in range(50):
        page += 1
        try:
            payload = client._request(
                method="GET",
                path="/api/v5/mktplace/nft/markets/collection-offers",
                params={
                    "chain": chain,
                    "maker": TARGET_WALLET,
                    "status": "active",
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            data = payload.get("data", {})
            items = data.get("data", []) if isinstance(data, dict) else []
            if not items:
                break
            for item in items:
                col_addr = (item.get("collectionAddress") or "").lower()
                price_raw = item.get("price", "0")
                cur_addr = (item.get("currencyAddress") or "").lower()
                cur_info = currencies.get(cur_addr, ("UNK", 18))
                cur_name, decimals = cur_info
                try:
                    price = int(price_raw) / (10 ** decimals) if price_raw.isdigit() else 0
                except:
                    price = 0
                all_offers.append({
                    "chain": chain,
                    "collection": col_addr,
                    "price": price,
                    "currency": cur_name,
                    "type": "collection-offer",
                })
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  collection-offers page {page} error: {e}")
            break

    print(f"  collection-offers: {sum(1 for o in all_offers if o['chain']==chain and o['type']=='collection-offer')} offers")

    # /offers endpoint (only ETH, BSC ignores maker filter)
    if chain != "bsc":
        cursor = None
        page = 0
        for _ in range(50):
            page += 1
            try:
                payload = client._request(
                    method="GET",
                    path="/api/v5/mktplace/nft/markets/offers",
                    params={
                        "chain": chain,
                        "maker": TARGET_WALLET,
                        "status": "active",
                        "limit": 100,
                        "cursor": cursor,
                    },
                )
                data = payload.get("data", {})
                items = data.get("data", []) if isinstance(data, dict) else []
                if not items:
                    break
                for item in items:
                    col_addr = (item.get("collectionAddress") or "").lower()
                    price_raw = item.get("price", "0")
                    cur_addr = (item.get("currencyAddress") or "").lower()
                    cur_info = currencies.get(cur_addr, ("UNK", 18))
                    cur_name, decimals = cur_info
                    try:
                        price = int(price_raw) / (10 ** decimals) if price_raw.isdigit() else 0
                    except:
                        price = 0
                    all_offers.append({
                        "chain": chain,
                        "collection": col_addr,
                        "price": price,
                        "currency": cur_name,
                        "type": "offer",
                    })
                cursor = data.get("cursor")
                if not cursor:
                    break
                time.sleep(0.3)
            except Exception as e:
                print(f"  offers page {page} error: {e}")
                break

        print(f"  offers: {sum(1 for o in all_offers if o['chain']==chain and o['type']=='offer')} offers")

    time.sleep(1)

print(f"\nTotal offers found: {len(all_offers)}")

# Group by collection, find max price per collection
collections = {}
for o in all_offers:
    key = o["collection"]
    if key not in collections:
        collections[key] = {"chain": o["chain"], "max_price": 0, "currency": o["currency"], "count": 0}
    collections[key]["count"] += 1
    if o["price"] > collections[key]["max_price"]:
        collections[key]["max_price"] = o["price"]
        collections[key]["currency"] = o["currency"]

# Try to resolve names from WL data
wl_path = Path("./data/binance_whitelist.json")
wl = json.loads(wl_path.read_text(encoding="utf-8"))
name_map = {c["contract_address"].lower(): c.get("collection_name", "") for c in wl}

# Also try okx_bsc_full.json
for fpath in ["./data/okx_bsc_full.json", "./data/okx_eth_full.json"]:
    try:
        full = json.loads(Path(fpath).read_text(encoding="utf-8"))
        for addr, info in full.items():
            name_map[addr.lower()] = info.get("name", "")
    except:
        pass

# Separate into existing and missing
missing = []
existing_in_config = []
for addr, info in sorted(collections.items(), key=lambda x: x[1]["max_price"], reverse=True):
    name = name_map.get(addr, addr[:14])
    entry = {
        "address": addr,
        "name": name,
        "chain": info["chain"],
        "max_price": info["max_price"],
        "currency": info["currency"],
        "num_offers": info["count"],
    }
    if addr in existing_addrs:
        existing_in_config.append(entry)
    else:
        missing.append(entry)

print(f"\n{'='*80}")
print(f"Collections ALREADY in buy_config: {len(existing_in_config)}")
for e in existing_in_config:
    print(f"  {e['name'][:35]:<35} {e['max_price']:>12.6f} {e['currency']:>6} ({e['chain']}) x{e['num_offers']}")

print(f"\n{'='*80}")
print(f"Collections MISSING from buy_config: {len(missing)}")
print(f"{'Collection':<35} {'MaxOffer':>12} {'Cur':>6} {'Chain':>6} {'#':>4}")
print("-" * 70)
for m in missing:
    print(f"{m['name'][:35]:<35} {m['max_price']:>12.6f} {m['currency']:>6} {m['chain']:>6} {m['num_offers']:>4}")

# Save missing to JSON for easy merging
out = {}
for m in missing:
    step = 0.01
    if m["currency"] in ("WBNB", "WETH"):
        step = 0.0001
    out[m["address"]] = {
        "name": m["name"],
        "chain": m["chain"],
        "enemy_max_price": m["max_price"],
        "max_offer_price": round(m["max_price"] + step, 6),
        "max_buy_price": round(m["max_price"] + step, 6),
        "currency": m["currency"],
        "num_enemy_offers": m["num_offers"],
        "enabled": True,
    }

out_path = Path("./config/wallet_missing_collections.json")
out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nMissing collections saved to {out_path} ({len(out)} entries)")
