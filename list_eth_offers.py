#!/usr/bin/env python3
"""List all active ETH offers to decide cleanup strategy."""
import sqlite3

c = sqlite3.connect('/app/data/execution.sqlite3')
print('=== ETH active offers ===')
rows = list(c.execute("""
    SELECT order_hash, collection, price_bnb, placed_at, current_floor
    FROM active_offers WHERE chain='eth' AND status='active'
    ORDER BY placed_at DESC
"""))
for r in rows:
    print(f"  {r[3]} coll={r[1][:20]} hash={r[0][:20]} price={r[2]}")
print(f"\nTotal: {len(rows)}")

print('\n=== Unique collections ===')
for r in c.execute("""
    SELECT collection, COUNT(*), SUM(price_bnb) FROM active_offers
    WHERE chain='eth' AND status='active' GROUP BY collection
"""):
    print(f"  {r[0]} count={r[1]} total_price={r[2]}")
