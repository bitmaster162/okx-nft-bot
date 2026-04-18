from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


_ALLOWED_SOURCE_MODES = {'trades', 'listings'}
_ALLOWED_MARKETS = {'okx', 'opensea', 'magiceden'}


@dataclass(slots=True, frozen=True)
class CollectionTarget:
    name: str
    market: str = 'okx'
    chain: str = 'eth'
    collection_address: str = ''
    collection_slug: str | None = None
    platform: str | None = None
    enabled: bool = True
    source_modes: tuple[str, ...] = ('trades',)
    min_price: float | None = None
    min_volume: float | None = None
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class CollectionRegistry:
    collections: list[CollectionTarget]

    @classmethod
    def from_path(cls, path: Path) -> 'CollectionRegistry':
        payload = json.loads(path.read_text(encoding='utf-8'))
        raw_collections = payload.get('collections', [])
        collections: list[CollectionTarget] = []
        for item in raw_collections:
            if not isinstance(item, dict):
                continue
            market = str(item.get('market', 'okx')).strip().lower()
            if market not in _ALLOWED_MARKETS:
                market = 'okx'
            source_modes = item.get('source_modes') or ['trades']
            collections.append(
                CollectionTarget(
                    name=str(item['name']),
                    market=market,
                    chain=str(item.get('chain', 'eth' if market == 'okx' else 'ethereum')),
                    collection_address=str(item.get('collection_address', '')),
                    collection_slug=(str(item['collection_slug']) if item.get('collection_slug') else None),
                    platform=(str(item['platform']) if item.get('platform') else None),
                    enabled=bool(item.get('enabled', True)),
                    source_modes=tuple(str(mode) for mode in source_modes if str(mode) in _ALLOWED_SOURCE_MODES) or ('trades',),
                    min_price=(float(item['min_price']) if item.get('min_price') is not None else None),
                    min_volume=(float(item['min_volume']) if item.get('min_volume') is not None else None),
                    tags=tuple(str(tag) for tag in item.get('tags', []) if str(tag)),
                )
            )
        return cls(collections=collections)

    def active(self) -> list[CollectionTarget]:
        return [collection for collection in self.collections if collection.enabled]

    def names(self) -> list[str]:
        return [collection.name for collection in self.active()]

    def get(self, name: str) -> CollectionTarget | None:
        for collection in self.collections:
            if collection.name == name:
                return collection
        return None
