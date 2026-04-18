#!/usr/bin/env python3
"""Find parasite 0xf1771c BSC offers by scanning collections.

Since OKX API doesn't support maker filter on BSC,
we scan each BSC WL collection and check who has offers.
"""
import json
import time
import os
from pathlib import Path

# Log to file
log_path = Path("./data/parasite_bsc_scan.log")
log_f = open(str(log_path), "w", buffering=1)

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line)
    log_f.write(line + "\n")

PARASITE = "0xf1771cf8831393422189330a79dd896223c357a4"

# Load OKX client
from okx_nft_bot.config import load_settings
from okx_nft_bot.clients.okx import OKXMarketplaceClient
settings = load_settings()
client = OKXMarketplaceClient(settings=settings)

# Load full OKX BSC list
okx_path = Path("./data/okx_bsc_full.json")
if okx_path.exists():
    okx_bsc = json.load(open(str(okx_path)))
    log(f"Loaded {len(okx_bsc)} OKX BSC collections")
else:
    log("ERROR: okx_bsc_full.json not found")
    exit(1)

# Load WL
wl_path = Path("./data/binance_whitelist.json")
wl = json.load(open(str(wl_path)))
wl_bsc = {item["contract_address"].lower() for item in wl
           if item.get("contract_address") and item.get("network", "").lower() == "bsc"}
log(f"BSC WL: {len(wl_bsc)}")

# Helper to fetch offers for a collection
def fetch_collection_offers(addr, chain="bsc"):
    """Fetch all offers for a collection, return list of (maker, price, currency)."""
    offers = []
    for path in ["/api/v5/mktplace/nft/markets/offers",
                 "/api/v5/mktplace/nft/markets/collection-offers"]:
        try:
            from okx_nft_bot.clients.http import StdlibHttpTransport, build_url
            url, request_path = build_url(
                settings.okx_api_base, path,
                {"chain": chain, "collectionAddress": addr, "status": "active", "limit": 100}
            )
            headers = client._build_headers(method="GET", request_path=request_path, body="")
            payload = client.transport.request_json(method="GET", url=url, headers=headers, body="")

            data = payload.get("data")
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("data", []) or data.get("offers", [])

            for o in items:
                maker = (o.get("makerAddress") or o.get("maker") or "").lower()
                price_raw = o.get("price") or o.get("offerPrice") or o.get("amount") or "0"
                currency = (o.get("currencyName") or o.get("currency") or o.get("paymentToken") or "").upper()
                try:
                    price = float(price_raw)
                except:
                    price = 0
                if maker and price > 0:
                    offers.append({"maker": maker, "price": price, "currency": currency})
        except Exception as e:
            pass
    return offers

# Scan all WL BSC collections for parasite offers
log(f"\n{'='*60}")
log(f"Scanning {len(wl_bsc)} BSC WL collections for parasite offers")
log(f"{'='*60}")

parasite_found = {}
all_wl_offers = {}

for i, addr in enumerate(sorted(wl_bsc)):
    offers = fetch_collection_offers(addr)
    if offers:
        all_wl_offers[addr] = offers
        parasite_offers = [o for o in offers if o["maker"] == PARASITE]
        if parasite_offers:
            name = "?"
            for item in wl:
                if item.get("contract_address", "").lower() == addr:
                    name = item.get("collection_name", "?")
                    break
            parasite_found[addr] = {"name": name, "offers": parasite_offers, "total_offers": len(offers)}
            prices_str = ", ".join(f"{o['price']:.4f} {o['currency']}" for o in parasite_offers)
            log(f"  [{i+1}/{len(wl_bsc)}] PARASITE FOUND: {name[:35]} - {len(parasite_offers)} offers, prices: [{prices_str}]")
        elif (i+1) % 20 == 0:
            log(f"  [{i+1}/{len(wl_bsc)}] scanning... ({len(parasite_found)} parasite collections found)")

    time.sleep(0.3)  # rate limit

log(f"\n{'='*60}")
log(f"RESULTS")
log(f"{'='*60}")
log(f"WL collections scanned: {len(wl_bsc)}")
log(f"WL collections with ANY offers: {len(all_wl_offers)}")
log(f"WL collections with PARASITE offers: {len(parasite_found)}")

if parasite_found:
    log(f"\nParasite 0xf1771c BSC collections:")
    for addr, info in sorted(parasite_found.items(), key=lambda x: x[1]["name"]):
        for o in info["offers"]:
            log(f"  {info['name'][:35]:<37} {o['price']:.6f} {o['currency']:<6} {addr}")

# Save results
out_path = Path("./data/parasite_bsc_found.json")
try:
    fh = open(str(out_path), "w", buffering=1)
    fh.write(json.dumps(parasite_found, indent=2, default=str))
    fh.flush()
    os.fsync(fh.fileno())
    fh.close()
    log(f"\nSaved to {out_path}")
except Exception as e:
    log(f"ERROR saving: {e}")

log("DONE")
