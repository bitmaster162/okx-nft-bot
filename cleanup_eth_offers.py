#!/usr/bin/env python3
"""Cancel stale ETH offers left over from the buggy ETH crawl period.

Usage: python3 cleanup_eth_offers.py [--dry-run]
"""
import sys
import sqlite3
import time

DRY = "--dry-run" in sys.argv

import os
sys.path.insert(0, '/app/src')

from okx_nft_bot.config import load_settings
from okx_nft_bot.counterbid.okx_api import OKXAPIClient

settings = load_settings()
api = OKXAPIClient(settings=settings)

db = sqlite3.connect('/app/data/execution.sqlite3')
rows = list(db.execute("""
    SELECT order_hash, collection, price_bnb, preview_payload_json
    FROM active_offers WHERE chain='eth' AND status='active'
    ORDER BY placed_at
"""))

print(f"Found {len(rows)} active ETH offers. DRY_RUN={DRY}")
print()

ok_count = 0
fail_count = 0

for i, (order_hash, collection, price, preview_json) in enumerate(rows, 1):
    print(f"[{i}/{len(rows)}] {collection[:20]} hash={order_hash[:20]} price={price}")
    if DRY:
        continue

    # Try to parse order_params from preview_payload_json (for on-chain fallback)
    order_params = None
    if preview_json:
        try:
            import json
            p = json.loads(preview_json)
            order_params = p.get("order_params") or p.get("order") or None
        except Exception:
            pass

    try:
        ok = api.cancel_offer(order_hash, chain="eth", order_params=order_params)
        if ok:
            print(f"    ✅ cancelled")
            db.execute("UPDATE active_offers SET status='cancelled' WHERE order_hash=?", (order_hash,))
            db.commit()
            ok_count += 1
        else:
            print(f"    ❌ failed")
            # Mark as exchange_missing so it stops being tracked
            db.execute("UPDATE active_offers SET status='exchange_missing' WHERE order_hash=?", (order_hash,))
            db.commit()
            fail_count += 1
    except Exception as exc:
        print(f"    ⚠ exception: {exc}")
        fail_count += 1

    time.sleep(1.0)  # rate limit

print()
print(f"=== Done: ok={ok_count} failed={fail_count} ===")
