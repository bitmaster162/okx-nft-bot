from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True, frozen=True)
class MarketSnapshot:
    market: str
    collection_key: str
    collection_name: str
    latest_event_time: datetime | None
    latest_listing_price: float | None
    latest_sale_price: float | None
    reference_price: float | None
    floor_price: float | None
    volume_24h: float | None
    event_count: int
    sale_count: int
    listing_count: int
    currency: str | None


@dataclass(slots=True, frozen=True)
class SpreadOpportunity:
    collection_key: str
    collection_name: str
    buy_market: str
    sell_market: str
    buy_price: float
    sell_price: float
    spread_abs: float
    spread_pct: float
    observed_at: datetime | None
    markets_seen: int
    reference_currency: str | None
    # Binance support metadata (v13)
    binance_supported: bool = False
    binance_list_type: str | None = None
    support_confidence: str = "unknown"
    support_source_url: str | None = None
    freshness: str | None = None
    confidence: str = "reference_static"


@dataclass(slots=True, frozen=True)
class CollectionScore:
    collection_key: str
    collection_name: str
    score: float
    market_count: int
    event_count: int
    listing_count: int
    sale_count: int
    volume_24h_total: float
    best_spread_pct: float
    latest_event_time: datetime | None
    signals: tuple[str, ...]
    # Binance support metadata (v13)
    binance_supported: bool = False
    binance_list_type: str | None = None
    support_confidence: str = "unknown"
    support_source_url: str | None = None
    freshness: str | None = None
    confidence: str = "reference_static"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _collection_key(row: dict[str, Any]) -> str:
    contract = str(row.get('contract_address') or '').strip().lower()
    if contract:
        return contract
    market = str(row.get('market') or 'unknown').strip().lower()
    name = str(row.get('collection_name') or row.get('collection') or 'unknown').strip().lower()
    return f'{market}:{name}'


def _collection_name(row: dict[str, Any]) -> str:
    return str(row.get('collection_name') or row.get('collection') or 'unknown')


def build_market_snapshots(rows: list[dict[str, Any]]) -> list[MarketSnapshot]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_collection_key(row), str(row.get('market') or 'unknown').strip().lower())
        grouped.setdefault(key, []).append(row)

    snapshots: list[MarketSnapshot] = []
    for (collection_key, market), items in grouped.items():
        latest_event_time = None
        latest_listing_price = None
        latest_sale_price = None
        floor_price = None
        volume_24h = None
        currency = None
        event_count = len(items)
        sale_count = 0
        listing_count = 0
        collection_name = _collection_name(items[0])

        for row in sorted(items, key=lambda item: _parse_dt(item.get('event_time')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            event_time = _parse_dt(row.get('event_time'))
            if latest_event_time is None and event_time is not None:
                latest_event_time = event_time
            event_type = str(row.get('event_type') or 'unknown').strip().lower()
            price = _to_float(row.get('price'))
            if currency is None and row.get('currency'):
                currency = str(row.get('currency'))
            if floor_price is None:
                floor_price = _to_float(row.get('floor_price'))
            if volume_24h is None:
                volume_24h = _to_float(row.get('volume_24h'))
            if event_type == 'sale':
                sale_count += 1
                if latest_sale_price is None and price is not None and price > 0:
                    latest_sale_price = price
            elif event_type == 'listing':
                listing_count += 1
                if latest_listing_price is None and price is not None and price > 0:
                    latest_listing_price = price

        reference_price = latest_listing_price or floor_price or latest_sale_price
        snapshots.append(MarketSnapshot(
            market=market,
            collection_key=collection_key,
            collection_name=collection_name,
            latest_event_time=latest_event_time,
            latest_listing_price=latest_listing_price,
            latest_sale_price=latest_sale_price,
            reference_price=reference_price,
            floor_price=floor_price,
            volume_24h=volume_24h,
            event_count=event_count,
            sale_count=sale_count,
            listing_count=listing_count,
            currency=currency,
        ))
    return snapshots


def detect_spreads(rows: list[dict[str, Any]], *, min_spread_pct: float = 3.0, top_n: int = 10, binance_mapper: Any = None) -> list[SpreadOpportunity]:
    snapshots = build_market_snapshots(rows)
    by_collection: dict[str, list[MarketSnapshot]] = {}
    for item in snapshots:
        by_collection.setdefault(item.collection_key, []).append(item)

    spreads: list[SpreadOpportunity] = []
    for collection_key, items in by_collection.items():
        eligible = [item for item in items if item.reference_price is not None and item.reference_price > 0]
        if len(eligible) < 2:
            continue
        # A7: group by currency before comparing. Comparing BNB to ETH as raw
        # floats gives garbage spreads (50% "arbitrage" that is actually just
        # the FX ratio). Only compare within one currency bucket.
        by_currency: dict[str, list[MarketSnapshot]] = {}
        for item in eligible:
            cur = (item.currency or "").upper().strip() or "UNKNOWN"
            by_currency.setdefault(cur, []).append(item)
        # Pick the bucket that has the most markets — that's the most
        # comparable view for this collection.
        best_bucket = max(by_currency.values(), key=len, default=[])
        if len(best_bucket) < 2:
            continue
        eligible = best_bucket
        buy_side = min(eligible, key=lambda item: item.reference_price or float('inf'))
        sell_side = max(eligible, key=lambda item: item.reference_price or 0.0)
        buy_price = float(buy_side.reference_price or 0.0)
        sell_price = float(sell_side.reference_price or 0.0)
        if buy_price <= 0 or sell_price <= 0 or sell_price <= buy_price:
            continue
        spread_abs = sell_price - buy_price
        spread_pct = (spread_abs / buy_price) * 100.0
        if spread_pct < min_spread_pct:
            continue
        observed_at = max((item.latest_event_time for item in eligible if item.latest_event_time is not None), default=None)
        binance_meta = _lookup_binance(binance_mapper, collection_key=collection_key, collection_name=buy_side.collection_name)
        spreads.append(SpreadOpportunity(
            collection_key=collection_key,
            collection_name=buy_side.collection_name,
            buy_market=buy_side.market,
            sell_market=sell_side.market,
            buy_price=buy_price,
            sell_price=sell_price,
            spread_abs=spread_abs,
            spread_pct=spread_pct,
            observed_at=observed_at,
            markets_seen=len(eligible),
            reference_currency=buy_side.currency or sell_side.currency,
            **binance_meta,
        ))

    spreads.sort(key=lambda item: (-item.spread_pct, -item.spread_abs, item.collection_name.lower()))
    return spreads[:max(top_n, 0)]


def rank_collections(rows: list[dict[str, Any]], *, min_spread_pct: float = 3.0, top_n: int = 10, binance_mapper: Any = None) -> list[CollectionScore]:
    snapshots = build_market_snapshots(rows)
    by_collection: dict[str, list[MarketSnapshot]] = {}
    for item in snapshots:
        by_collection.setdefault(item.collection_key, []).append(item)

    spreads = {item.collection_key: item for item in detect_spreads(rows, min_spread_pct=min_spread_pct, top_n=max(len(by_collection), top_n))}
    now = datetime.now(timezone.utc)
    ranked: list[CollectionScore] = []
    for collection_key, items in by_collection.items():
        latest_event_time = max((item.latest_event_time for item in items if item.latest_event_time is not None), default=None)
        event_count = sum(item.event_count for item in items)
        listing_count = sum(item.listing_count for item in items)
        sale_count = sum(item.sale_count for item in items)
        market_count = len(items)
        volume_24h_total = sum(item.volume_24h or 0.0 for item in items)
        spread_pct = spreads[collection_key].spread_pct if collection_key in spreads else 0.0
        freshness_hours = 9999.0 if latest_event_time is None else max((now - latest_event_time).total_seconds() / 3600.0, 0.0)
        freshness_score = max(0.0, 1.0 - min(freshness_hours / 48.0, 1.0))
        activity_score = min(event_count / 20.0, 1.0)
        sale_score = min(sale_count / 10.0, 1.0)
        volume_score = min(volume_24h_total / 100.0, 1.0)
        diversity_score = min(market_count / 3.0, 1.0)
        spread_score = min(spread_pct / 15.0, 1.0)
        score = 100.0 * (
            0.22 * freshness_score +
            0.18 * activity_score +
            0.18 * sale_score +
            0.20 * volume_score +
            0.12 * diversity_score +
            0.10 * spread_score
        )
        signals: list[str] = []
        if spread_pct >= min_spread_pct:
            signals.append(f'spread={spread_pct:.2f}%')
        if market_count >= 2:
            signals.append(f'markets={market_count}')
        if volume_24h_total > 0:
            signals.append(f'vol24h={volume_24h_total:.2f}')
        if freshness_hours <= 6:
            signals.append('fresh<=6h')
        binance_meta = _lookup_binance(binance_mapper, collection_key=collection_key, collection_name=items[0].collection_name)
        ranked.append(CollectionScore(
            collection_key=collection_key,
            collection_name=items[0].collection_name,
            score=round(score, 2),
            market_count=market_count,
            event_count=event_count,
            listing_count=listing_count,
            sale_count=sale_count,
            volume_24h_total=round(volume_24h_total, 4),
            best_spread_pct=round(spread_pct, 4),
            latest_event_time=latest_event_time,
            signals=tuple(signals),
            **binance_meta,
        ))

    ranked.sort(key=lambda item: (-item.score, -item.best_spread_pct, -item.event_count, item.collection_name.lower()))
    return ranked[:max(top_n, 0)]


def _to_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lookup_binance(binance_mapper: Any, *, collection_key: str, collection_name: str) -> dict[str, Any]:
    """Safe Binance support lookup — never raises, returns unknown defaults on error."""
    if binance_mapper is None:
        return {
            "binance_supported": False,
            "binance_list_type": None,
            "support_confidence": "unknown",
            "support_source_url": None,
            "freshness": None,
            "confidence": "reference_static",
        }
    try:
        return binance_mapper.lookup_dict(address=collection_key, name=collection_name)
    except Exception:
        return {
            "binance_supported": False,
            "binance_list_type": None,
            "support_confidence": "unknown",
            "support_source_url": None,
            "freshness": None,
            "confidence": "reference_static",
        }
