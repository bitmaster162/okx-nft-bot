#!/usr/bin/env python3
"""Trace why Highstreet (0x1b26) generates 9.3 BNB offers."""
import json

target = '0x1b26e0f75c623fe9357dbc6c1871ab745faccf04'

print('=== buy_config.json ===')
with open('/app/config/buy_config.json') as f:
    bc = json.load(f)

entry = bc.get('collections', {}).get(target)
if entry:
    print(json.dumps(entry, indent=2))
else:
    print('NOT FOUND')

print('\n=== binance_whitelist.json ===')
try:
    with open('/app/data/binance_whitelist.json') as f:
        wl = json.load(f)
    found = []
    if isinstance(wl, list):
        for item in wl:
            if isinstance(item, dict) and (item.get('contract_address') or '').lower() == target:
                found.append(item)
    elif isinstance(wl, dict):
        for k, v in wl.items():
            if isinstance(v, dict):
                if (v.get('contract_address') or '').lower() == target:
                    found.append({**v, '_key': k})
    for f in found:
        print(json.dumps(f, indent=2))
except Exception as e:
    print(f'Error: {e}')
