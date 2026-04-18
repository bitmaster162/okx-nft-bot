from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class SniperTarget:
    name: str
    enabled: bool
    market: str
    chain: str
    collection_address: str
    collection_slug: str | None
    buy_below_price: float
    relist_price: float
    currency: str
    max_buys_per_cycle: int
    max_total_buys: int
    min_relist_profit_pct: float


def load_sniper_config(path: Path) -> list[SniperTarget]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    targets = []
    for item in data.get('sniper_targets', []):
        if not item.get('enabled', False):
            continue
        targets.append(SniperTarget(
            name=str(item['name']),
            enabled=bool(item.get('enabled', False)),
            market=str(item.get('market', 'okx')),
            chain=str(item.get('chain', 'bsc')),
            collection_address=str(item['collection_address']),
            collection_slug=item.get('collection_slug'),
            buy_below_price=float(item['buy_below_price']),
            relist_price=float(item['relist_price']),
            currency=str(item.get('currency', 'BNB')),
            max_buys_per_cycle=int(item.get('max_buys_per_cycle', 1)),
            max_total_buys=int(item.get('max_total_buys', 10)),
            min_relist_profit_pct=float(item.get('min_relist_profit_pct', 0)),
        ))
    return targets
