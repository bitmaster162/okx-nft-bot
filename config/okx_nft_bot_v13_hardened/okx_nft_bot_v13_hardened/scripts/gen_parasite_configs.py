#!/usr/bin/env python3
"""
Generate buy_config entries from parasite wallet portfolios.

Scans all parasite wallets' active offers on OKX, finds collections they're
bidding on, and generates buy_config entries with:
  - max_offer_price = parasite's highest offer price * 1.05 (+5%)
  - max_buy_price = same as max_offer_price
  - low_price = max_offer_price * 1.10 (sell floor)
  - currency = same as parasite's offer currency

Usage:
  python scripts/gen_parasite_configs.py [--dry-run] [--merge]

  --dry-run   Print what would be added, don't write
  --merge     Merge into existing buy_config.json (default: just print)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_parasite_wallets() -> list[str]:
    """Load parasite wallets from .env or parasite.txt."""
    # Try .env
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)

    raw = os.getenv("PARASITE_WALLETS", "")
    if raw:
        wallets = [w.strip().lower() for w in raw.split(",") if w.strip()]
        if wallets:
            return wallets

    # Fallback to parasite.txt
    txt_path = Path(__file__).resolve().parent.parent / "parasite.txt"
    if txt_path.exists():
        wallets = [
            line.strip().lower()
            for line in txt_path.read_text().splitlines()
            if line.strip() and line.strip().startswith("0x")
        ]
        return wallets

    return []


def get_okx_client():
    """Initialize OKX API client."""
    from okx_nft_bot.config import load_settings
    from okx_nft_bot.clients.okx import OKXMarketplaceClient

    settings = load_settings()
    return OKXMarketplaceClient(settings=settings)


def fetch_wallet_offers(client, wallet: str, chain: str) -> list[dict]:
    """Fetch all active offers for a wallet on a chain."""
    all_offers = []

    # Collection offers
    try:
        cursor = None
        for _ in range(10):
            resp = client.get_collection_offers(
                chain=chain,
                maker=wallet,
                status="active",
                limit=100,
                cursor=cursor,
            )
            data = resp.get("data", {})
            items = data.get("data", []) if isinstance(data, dict) else []
            if not items:
                break
            all_offers.extend(items)
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.3)
    except Exception as e:
        log.warning("  Collection offers fetch error for %s on %s: %s", wallet[:10], chain, e)

    # Token-level offers (ETH only; BSC API doesn't support maker filter)
    if chain != "bsc":
        try:
            cursor = None
            for _ in range(10):
                payload = client.get_offers(
                    chain=chain,
                    maker=wallet,
                    status="active",
                    limit=100,
                    cursor=cursor,
                )
                data = payload.get("data", {})
                items = data.get("data", []) if isinstance(data, dict) else []
                if not items:
                    break
                all_offers.extend(items)
                cursor = data.get("cursor")
                if not cursor:
                    break
                time.sleep(0.3)
        except Exception as e:
            log.warning("  Token offers fetch error for %s on %s: %s", wallet[:10], chain, e)

    return all_offers


def _wei_to_human(price_raw: float, currency: str, chain: str) -> float:
    """Convert wei/raw price to human-readable value.

    OKX API returns prices in smallest unit (wei).
    Decimals:
      - ETH chain: USDT/USDC = 6 decimals, everything else = 18
      - BSC chain: all Seaport currencies = 18 decimals
    If the price is already human-scale (< 1e9), return as-is.
    """
    if price_raw < 1_000_000_000:  # already human-readable
        return price_raw
    if chain == "eth" and currency in ("USDT", "USDC"):
        return price_raw / 1e6
    return price_raw / 1e18


def parse_offer_entry(raw: dict, chain: str = "bsc") -> dict | None:
    """Parse a raw OKX offer into structured data."""
    collection_addr = (
        raw.get("collectionAddress")
        or raw.get("collection_address")
        or raw.get("nftAddress")
        or ""
    ).lower()

    if not collection_addr:
        return None

    price_raw = float(raw.get("price") or raw.get("offerPrice") or 0)
    currency = (
        raw.get("currencyName")
        or raw.get("currency")
        or raw.get("paymentToken", {}).get("symbol", "")
        or ""
    ).upper()

    # Convert from wei to human-readable
    price = _wei_to_human(price_raw, currency, chain)

    name = (
        raw.get("collectionName")
        or raw.get("collection_name")
        or raw.get("nftName")
        or collection_addr[:14]
    )

    if price <= 0:
        return None

    return {
        "collection_address": collection_addr,
        "name": name,
        "price": price,
        "currency": currency,
        "maker": (raw.get("maker") or raw.get("offerAddress") or "").lower(),
    }


def main():
    dry_run = "--dry-run" in sys.argv
    merge = "--merge" in sys.argv

    wallets = load_parasite_wallets()
    # Remove our own wallet
    our_wallet = os.getenv("BUYER_WALLET_ADDRESS", "").strip().lower()
    wallets = [w for w in wallets if w != our_wallet]

    log.info("Loaded %d parasite wallets", len(wallets))

    client = get_okx_client()

    # Collect max price per collection across all parasites
    # Key: collection_address → {name, max_price, currency, parasites}
    collection_data: dict[str, dict] = {}

    for i, wallet in enumerate(wallets):
        log.info("[%d/%d] Scanning %s...", i + 1, len(wallets), wallet[:14])
        for chain in ("bsc", "eth"):
            offers = fetch_wallet_offers(client, wallet, chain)
            log.info("  %s: %d offers", chain.upper(), len(offers))

            for raw in offers:
                parsed = parse_offer_entry(raw, chain=chain)
                if not parsed:
                    continue

                addr = parsed["collection_address"]
                if addr not in collection_data:
                    collection_data[addr] = {
                        "name": parsed["name"],
                        "max_price": parsed["price"],
                        "currency": parsed["currency"],
                        "chain": chain,
                        "parasites": {wallet},
                    }
                else:
                    existing = collection_data[addr]
                    # Update max price if this parasite bids higher
                    if parsed["price"] > existing["max_price"]:
                        existing["max_price"] = parsed["price"]
                        existing["currency"] = parsed["currency"]
                    existing["parasites"].add(wallet)

            time.sleep(1.0)  # Rate limit between chains
        time.sleep(1.5)  # Rate limit between wallets

    log.info("\nFound %d unique collections across all parasites", len(collection_data))

    # Load existing config
    config_path = Path(__file__).resolve().parent.parent / "config" / "buy_config.json"
    with open(config_path) as f:
        cfg = json.load(f)

    existing_collections = cfg.get("collections", {})
    new_entries = {}
    updated_entries = {}

    for addr, data in sorted(collection_data.items(), key=lambda x: -x[1]["max_price"]):
        # Calculate our prices: +5% above max parasite offer
        our_max = round(data["max_price"] * 1.05, 6)
        our_low = round(our_max * 1.10, 6)

        entry = {
            "name": data["name"],
            "max_offer_price": our_max,
            "max_buy_price": our_max,
            "currency": data["currency"],
            "low_price": our_low,
            "enabled": True,
            "_parasites": len(data["parasites"]),
            "_source": "auto_parasite_scan",
        }

        if addr in existing_collections:
            # Already exists — check if our new price is higher
            ex = existing_collections[addr]
            ex_max = ex.get("max_offer_price", 0) or ex.get("max_buy_price", 0)
            if our_max > ex_max:
                updated_entries[addr] = entry
                log.info("  UPDATE %s: %s → max=%.4f %s (was %.4f)",
                         addr[:14], data["name"], our_max, data["currency"], ex_max)
        else:
            new_entries[addr] = entry
            log.info("  NEW    %s: %s → max=%.4f %s (parasites=%d)",
                     addr[:14], data["name"], our_max, data["currency"],
                     len(data["parasites"]))

    log.info("\nSummary:")
    log.info("  New collections: %d", len(new_entries))
    log.info("  Updated collections: %d", len(updated_entries))
    log.info("  Already configured (no change): %d",
             len(collection_data) - len(new_entries) - len(updated_entries))

    if dry_run:
        log.info("\n--- DRY RUN: printing new entries ---")
        for addr, entry in new_entries.items():
            e = dict(entry)
            e.pop("_parasites", None)
            e.pop("_source", None)
            print(f'    "{addr}": {json.dumps(e, ensure_ascii=False)}')
        return

    if merge:
        # Merge into existing config
        for addr, entry in {**new_entries, **updated_entries}.items():
            clean = dict(entry)
            clean.pop("_parasites", None)
            clean.pop("_source", None)
            if addr in existing_collections:
                # Preserve existing fields, update prices
                existing_collections[addr].update(clean)
            else:
                existing_collections[addr] = clean

        # Write compact JSON to reduce file size (Google Drive sync
        # truncates files >~75KB).  Top-level keys are pretty-printed,
        # but each collection is a single line to save space.
        import shutil

        lines = ["{\n"]
        top_keys = [k for k in cfg if k != "collections"]
        for i, k in enumerate(top_keys):
            comma = "," if (i < len(top_keys) - 1 or cfg.get("collections")) else ""
            lines.append(f"  {json.dumps(k)}: {json.dumps(cfg[k], ensure_ascii=False)}{comma}\n")
        if "collections" in cfg:
            lines.append('  "collections": {\n')
            items = list(cfg["collections"].items())
            for j, (addr, entry) in enumerate(items):
                comma = "," if j < len(items) - 1 else ""
                lines.append(f"    {json.dumps(addr)}: {json.dumps(entry, ensure_ascii=False)}{comma}\n")
            lines.append("  }\n")
        lines.append("}\n")

        content = "".join(lines)

        # Validate before writing
        json.loads(content)

        # Write to system TEMP first (real local disk), then copy to
        # Google Drive path.  Drive's virtual FS truncates large writes.
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="buy_config_")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # Verify tmp file size matches expected
        expected_size = len(content.encode("utf-8"))
        actual_size = os.path.getsize(tmp_path)
        if actual_size != expected_size:
            os.unlink(tmp_path)
            raise RuntimeError(f"Temp file size mismatch: {actual_size} vs {expected_size}")

        shutil.copy2(tmp_path, str(config_path))
        os.unlink(tmp_path)

        # Verify destination
        dest_size = os.path.getsize(str(config_path))
        if dest_size != expected_size:
            log.error("DESTINATION SIZE MISMATCH: %d vs %d — Google Drive may have truncated!", dest_size, expected_size)
        else:
            log.info("File written OK: %d bytes", dest_size)

        log.info("Merged %d entries into %s",
                 len(new_entries) + len(updated_entries), config_path)
    else:
        # Print as JSON for manual review
        output = {}
        for addr, entry in {**new_entries, **updated_entries}.items():
            clean = dict(entry)
            clean.pop("_parasites", None)
            clean.pop("_source", None)
            output[addr] = clean
        print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
