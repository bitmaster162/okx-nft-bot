#!/usr/bin/env python3
"""Parse wallet offers and MERGE missing collections into buy_config.json.
Sets max_price = enemy_max + 3 steps (not enemy_max + 1 step).

Usage:
  docker exec okx-nft-bot-sales-stream python3 /app/config/parse_and_merge_wallet.py
"""
import json, sys, time
sys.path.insert(0, "/app/src")
from pathlib import Path

TARGET_WALLET = "0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7"

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

STEPS = {"WBNB": 0.0001, "WETH": 0.0001, "USDT": 0.01, "USDC": 0.01, "BUSD": 0.01, "DAI": 0.01}

all_offers = []

for chain, currencies in [("bsc", BSC_CURRENCIES), ("eth", ETH_CURRENCIES)]:
    print(f"=== Scanning {chain.upper()} ===")
    cursor = None
    for _ in range(50):
        try:
            payload = client._request(
                method="GET",
                path="/api/v5/mktplace/nft/markets/collection-offers",
                params={"chain": chain, "maker": TARGET_WALLET, "status": "active", "limit": 100, "cursor": cursor},
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
                all_offers.append({"chain": chain, "collection": col_addr, "price": price, "currency": cur_name})
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"  error: {e}")
            break

    if chain != "bsc":
        cursor = None
        for _ in range(50):
            try:
                payload = client._request(
                    method="GET",
                    path="/api/v5/mktplace/nft/markets/offers",
                    params={"chain": chain, "maker": TARGET_WALLET, "status": "active", "limit": 100, "cursor": cursor},
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
                    all_offers.append({"chain": chain, "collection": col_addr, "price": price, "currency": cur_name})
                cursor = data.get("cursor")
                if not cursor:
                    break
                time.sleep(0.3)
            except Exception as e:
                print(f"  error: {e}")
                break
    time.sleep(1)

print(f"\nTotal offers: {len(all_offers)}")

# Group by collection
collections = {}
for o in all_offers:
    key = o["collection"]
    if key not in collections:
        collections[key] = {"chain": o["chain"], "max_price": 0, "currency": o["currency"], "count": 0}
    collections[key]["count"] += 1
    if o["price"] > collections[key]["max_price"]:
        collections[key]["max_price"] = o["price"]
        collections[key]["currency"] = o["currency"]

# Resolve names
wl_path = Path("./data/binance_whitelist.json")
wl = json.loads(wl_path.read_text(encoding="utf-8"))
name_map = {c["contract_address"].lower(): c.get("collection_name", "") for c in wl}
for fpath in ["./data/okx_bsc_full.json", "./data/okx_eth_full.json"]:
    try:
        full = json.loads(Path(fpath).read_text(encoding="utf-8"))
        for addr, info in full.items():
            name_map[addr.lower()] = info.get("name", "")
    except:
        pass

# Merge ONLY missing collections
added = 0
already = 0
for addr, info in sorted(collections.items(), key=lambda x: x[1]["max_price"], reverse=True):
    if addr in existing_addrs:
        already += 1
        continue

    name = name_map.get(addr, addr[:14])
    step = STEPS.get(info["currency"], 0.01)
    # max_price = enemy + 3 steps (not 1!)
    max_price = round(info["max_price"] + 3 * step, 6)

    entry = {
        "name": name,
        "max_offer_price": max_price,
        "max_buy_price": max_price,
        "currency": info["currency"],
        "enabled": True,
    }

    # USD estimate for max_offers
    cur = info["currency"]
    if cur in ("WBNB", "BNB"):
        usd = max_price * 600
    elif cur in ("WETH", "ETH"):
        usd = max_price * 2100
    else:
        usd = max_price
    if usd < 0.51:
        entry["max_offers"] = 10

    config["collections"][addr] = entry
    added += 1
    print(f"  + {name[:35]:<35} enemy={info['max_price']:.4f} max={max_price:.4f} {cur} (x{info['count']})")

config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nAdded {added} new, {already} already existed")
print(f"Total collections: {len(config['collections'])}")
