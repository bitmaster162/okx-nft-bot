from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from okx_nft_bot.models import NFTEvent


@dataclass(slots=True)
class RulePack:
    name: str
    enabled: bool = True
    collections: tuple[str, ...] = ()
    collection_contains: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()
    min_price: float | None = None
    min_volume: float | None = None

    def matches(self, event: NFTEvent) -> bool:
        if not self.enabled:
            return False
        collection_lc = event.collection.lower()
        if self.collections and event.collection not in self.collections:
            return False
        if self.collection_contains and not any(part.lower() in collection_lc for part in self.collection_contains):
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.markets and event.market not in self.markets:
            return False
        if self.min_price is not None and (event.price is None or event.price < self.min_price):
            return False
        if self.min_volume is not None and (event.volume_24h is None or event.volume_24h < self.min_volume):
            return False
        return True


def load_rule_packs(path: Path) -> list[RulePack]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("rule_packs", []) if isinstance(payload, dict) else []
    packs: list[RulePack] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        packs.append(
            RulePack(
                name=str(item["name"]),
                enabled=bool(item.get("enabled", True)),
                collections=tuple(str(x) for x in item.get("collections", []) if x),
                collection_contains=tuple(str(x) for x in item.get("collection_contains", []) if x),
                event_types=tuple(str(x) for x in item.get("event_types", []) if x),
                markets=tuple(str(x) for x in item.get("markets", []) if x),
                min_price=float(item["min_price"]) if item.get("min_price") is not None else None,
                min_volume=float(item["min_volume"]) if item.get("min_volume") is not None else None,
            )
        )
    return packs
