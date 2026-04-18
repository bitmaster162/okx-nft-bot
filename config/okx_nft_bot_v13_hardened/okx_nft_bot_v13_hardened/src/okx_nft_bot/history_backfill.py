from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.magiceden import MagicEdenClient
from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.models import NFTEvent, RawEvent
from okx_nft_bot.pipeline.normalize import normalize_many
from okx_nft_bot.storage.sqlite import SQLiteStore


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        return [data]
    return []


def _extract_cursor(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        cursor = data.get("cursor") or data.get("nextCursor")
        return None if cursor in {None, "", "0"} else str(cursor)
    cursor = payload.get("cursor") or payload.get("nextCursor")
    return None if cursor in {None, "", "0"} else str(cursor)


def _extract_next(payload: dict[str, Any], cursor: str | None) -> bool:
    data = payload.get("data")
    if isinstance(data, dict) and "next" in data:
        return bool(data.get("next"))
    return cursor is not None


def _timestamp_to_iso(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    ts = int(value)
    if ts > 10_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_str(values: Iterable[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _collection_address(item: dict[str, Any]) -> str | None:
    direct = _first_str(
        (
            item.get("collectionAddress"),
            item.get("contractAddress"),
        )
    )
    if direct:
        return direct

    collection = item.get("collection")
    if isinstance(collection, dict):
        nested = _first_str(
            (
                collection.get("collectionAddress"),
                collection.get("contractAddress"),
            )
        )
        if nested:
            return nested
        asset_contract = collection.get("assetContract")
        if isinstance(asset_contract, dict):
            nested = _first_str((asset_contract.get("contractAddress"),))
            if nested:
                return nested
        asset_contracts = collection.get("assetContracts")
        if isinstance(asset_contracts, list):
            for asset in asset_contracts:
                if isinstance(asset, dict):
                    nested = _first_str((asset.get("contractAddress"),))
                    if nested:
                        return nested

    asset_contract = item.get("assetContract")
    if isinstance(asset_contract, dict):
        nested = _first_str((asset_contract.get("contractAddress"),))
        if nested:
            return nested

    asset_contracts = item.get("assetContracts")
    if isinstance(asset_contracts, list):
        for asset in asset_contracts:
            if isinstance(asset, dict):
                nested = _first_str((asset.get("contractAddress"),))
                if nested:
                    return nested
    return None


def _collection_name(item: dict[str, Any], contract_address: str) -> str:
    collection = item.get("collection")
    if isinstance(collection, dict):
        name = _first_str((collection.get("name"), collection.get("slug")))
        if name:
            return name
    name = _first_str((item.get("name"), item.get("slug"), item.get("collectionName")))
    return name or contract_address


def _build_trade_event_id(trade: dict[str, Any]) -> str:
    return f"sale:{trade.get('txHash', 'nohash')}:{trade.get('tokenId', 'unknown')}:{trade.get('timestamp', '0')}"


def _trade_raw_event(
    *,
    trade: dict[str, Any],
    collection_name: str,
    floor_price: float | None,
    volume_24h: float | None,
    next_cursor: str | None,
) -> RawEvent:
    payload = {
        "market": "okx",
        "event_type": "sale",
        "event_id": _build_trade_event_id(trade),
        "collection": collection_name,
        "token_id": str(trade.get("tokenId", "unknown")),
        "contract_address": trade.get("collectionAddress"),
        "price": _to_float(trade.get("price")),
        "currency": trade.get("currencyAddress"),
        "quantity": int(trade.get("amount", 1) or 1),
        "maker": trade.get("from"),
        "taker": trade.get("to"),
        "tx_hash": trade.get("txHash"),
        "event_time": _timestamp_to_iso(trade.get("timestamp")),
        "floor_price": floor_price,
        "volume_24h": volume_24h,
        "trade": trade,
        "next_cursor": next_cursor,
    }
    return RawEvent(source="okx_marketplace_history", payload=payload)


def _persist_raw_events(store: SQLiteStore, raw_events: list[RawEvent]) -> tuple[int, int, int]:
    if not raw_events:
        return 0, 0, 0
    store.insert_raw_events(raw_events)
    normalized = normalize_many(raw_events)
    new_count = len(store.filter_new_events(normalized))
    store.upsert_normalized_events(normalized)
    return len(raw_events), len(normalized), new_count


def _map_market_event_type(raw_type: str | None) -> str:
    text = (raw_type or "").strip().lower()
    if any(part in text for part in ("sale", "sold", "successful")):
        return "sale"
    if any(part in text for part in ("listing", "listed", "ask")):
        return "listing"
    if any(part in text for part in ("offer", "bid")):
        return "bid"
    if "transfer" in text:
        return "transfer"
    return "unknown"


def _opensea_extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("asset_events")
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    events = payload.get("events")
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    return []


def _opensea_next_cursor(payload: dict[str, Any]) -> str | None:
    for key in ("next", "next_cursor", "cursor"):
        value = payload.get(key)
        if value not in {None, "", "0"}:
            return str(value)
    return None


def _opensea_extract_nft(item: dict[str, Any]) -> dict[str, Any]:
    nft = item.get("nft")
    if isinstance(nft, dict):
        return nft
    item_block = item.get("item")
    if isinstance(item_block, dict):
        nft = item_block.get("nft")
        if isinstance(nft, dict):
            return nft
    return {}


def _opensea_extract_address(item: dict[str, Any], key: str) -> str | None:
    candidate = item.get(key)
    if isinstance(candidate, dict):
        return _first_str((candidate.get("address"), candidate.get("wallet_address"), candidate.get("username")))
    if candidate:
        return str(candidate)
    return None


def _opensea_money_value(value: Any) -> float | None:
    if isinstance(value, dict):
        return _to_float(value.get("current") or value.get("amount") or value.get("quantity") or value.get("value"))
    return _to_float(value)


def _opensea_currency_symbol(item: dict[str, Any]) -> str | None:
    payment = item.get("payment")
    if isinstance(payment, dict):
        symbol = _first_str((payment.get("symbol"), payment.get("token"), payment.get("quantity_type")))
        if symbol:
            return symbol
    price = item.get("price")
    if isinstance(price, dict):
        symbol = _first_str((price.get("currency"),))
        if symbol:
            return symbol
    return None


def _build_opensea_history_event(item: dict[str, Any], slug: str, event_type_hint: str | None, collection_name: str, floor_price: float | None, volume_24h: float | None) -> RawEvent:
    nft = _opensea_extract_nft(item)
    raw_event_type = _first_str((event_type_hint, item.get("event_type"), item.get("eventName"), item.get("type"))) or "unknown"
    token_id = str(nft.get("identifier") or nft.get("token_id") or item.get("token_id") or item.get("identifier") or "unknown")
    contract_address = _first_str((nft.get("contract"), nft.get("contract_address"), item.get("contract_address")))
    event_time = _first_str((item.get("event_timestamp"), item.get("created_date"), item.get("listed_date"), item.get("created_at")))
    event_type = _map_market_event_type(raw_event_type)
    event_id = _first_str((item.get("event_id"), item.get("order_hash"), item.get("transaction"), item.get("transaction_hash")))
    if not event_id:
        event_id = f"oshist:{raw_event_type}:{token_id}:{event_time or '0'}"
    payload = {
        "market": "opensea",
        "event_type": event_type,
        "event_id": str(event_id),
        "collection": collection_name or slug,
        "token_id": token_id,
        "contract_address": contract_address,
        "price": _opensea_money_value(item.get("sale_price") or item.get("price")),
        "currency": _opensea_currency_symbol(item),
        "quantity": int(item.get("quantity") or item.get("asset_quantity") or 1),
        "maker": _opensea_extract_address(item, "seller") or _opensea_extract_address(item, "from_account"),
        "taker": _opensea_extract_address(item, "buyer") or _opensea_extract_address(item, "to_account"),
        "tx_hash": _first_str((item.get("transaction"), item.get("transaction_hash"))),
        "event_time": event_time or datetime.now(timezone.utc).isoformat(),
        "floor_price": floor_price,
        "volume_24h": volume_24h,
        "source_event_type": raw_event_type,
        "event": item,
    }
    return RawEvent(source="opensea_collection_history", payload=payload)


def _magiceden_extract_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = payload.get(key)
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _build_magiceden_history_event(item: dict[str, Any], collection: str) -> RawEvent:
    raw_type = _first_str((item.get("type"), item.get("kind"), item.get("eventType"))) or "unknown"
    token = item.get("token") if isinstance(item.get("token"), dict) else {}
    token_id = str(
        token.get("tokenId")
        or item.get("tokenId")
        or item.get("token", {}).get("tokenId") if isinstance(item.get("token"), dict) else "unknown"
    )
    contract_address = _first_str((item.get("contract"), token.get("contract")))
    if token_id == "unknown":
        token_set_id = _first_str((item.get("tokenSetId"),))
        if token_set_id:
            parts = token_set_id.split(":")
            if len(parts) >= 3:
                contract_address = contract_address or parts[1]
                token_id = parts[-1]
    price_block = item.get("price") if isinstance(item.get("price"), dict) else {}
    amount_block = price_block.get("amount") if isinstance(price_block.get("amount"), dict) else {}
    currency_block = price_block.get("currency") if isinstance(price_block.get("currency"), dict) else {}
    collection_name = collection
    token_collection = token.get("collection")
    if isinstance(token_collection, dict):
        collection_name = _first_str((token_collection.get("name"), token_collection.get("id"))) or collection
    event_time = _first_str((item.get("timestamp"), item.get("createdAt"), item.get("updatedAt"), item.get("validFrom")))
    event_id = _first_str((item.get("id"), item.get("txHash"), item.get("orderId")))
    if not event_id:
        event_id = f"mehist:{raw_type}:{contract_address or collection}:{token_id}:{event_time or '0'}"
    payload = {
        "market": "magiceden",
        "event_type": _map_market_event_type(raw_type),
        "event_id": str(event_id),
        "collection": collection_name,
        "token_id": token_id,
        "contract_address": contract_address,
        "price": _to_float(amount_block.get("native") or amount_block.get("decimal") or price_block.get("amount")),
        "currency": _first_str((currency_block.get("symbol"), currency_block.get("contract"), "ETH")),
        "quantity": int(item.get("quantityRemaining") or item.get("quantity") or 1),
        "maker": _first_str((item.get("maker"), item.get("fromAddress"), item.get("seller"))),
        "taker": _first_str((item.get("taker"), item.get("toAddress"), item.get("buyer"))),
        "tx_hash": _first_str((item.get("txHash"),)),
        "event_time": _timestamp_to_iso(event_time) if event_time and str(event_time).isdigit() else (str(event_time).replace("Z", "+00:00") if event_time and "T" in str(event_time) else datetime.now(timezone.utc).isoformat()),
        "floor_price": None,
        "volume_24h": None,
        "source_event_type": raw_type,
        "activity": item,
    }
    return RawEvent(source="magiceden_activity_history", payload=payload)


def backfill_okx_sales_history(
    *,
    client: OKXMarketplaceClient,
    store: SQLiteStore,
    chain: str,
    platform: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    collection_page_limit: int = 300,
    trade_page_limit: int = 50,
    max_collections: int | None = None,
    max_trade_pages_per_collection: int | None = None,
) -> dict[str, Any]:
    collection_cursor: str | None = None
    processed_collections = 0
    skipped_collections = 0
    raw_event_count = 0
    normalized_event_count = 0
    new_event_count = 0
    collections_with_sales = 0
    collection_errors: list[dict[str, str]] = []

    while True:
        payload = client.get_collection_list(chain=chain, limit=collection_page_limit, cursor=collection_cursor)
        collection_rows = _extract_records(payload)
        collection_cursor = _extract_cursor(payload)
        has_next_collections = _extract_next(payload, collection_cursor)

        for row in collection_rows:
            if max_collections is not None and processed_collections >= max_collections:
                has_next_collections = False
                break

            contract_address = _collection_address(row)
            if not contract_address:
                skipped_collections += 1
                continue

            processed_collections += 1
            collection_name = _collection_name(row, contract_address)
            floor_price = _to_float(row.get("floorPrice"))
            volume_24h = _to_float(row.get("volume24h"))
            trade_cursor: str | None = None
            trade_pages = 0
            collection_sale_count = 0

            try:
                while True:
                    trade_payload = client.get_collection_trades(
                        chain=chain,
                        collection_address=contract_address,
                        platform=platform,
                        limit=trade_page_limit,
                        cursor=trade_cursor,
                        start_time=start_time,
                        end_time=end_time,
                    )
                    trade_rows = _extract_records(trade_payload)
                    trade_cursor = _extract_cursor(trade_payload)
                    has_next_trades = _extract_next(trade_payload, trade_cursor)
                    trade_pages += 1

                    if trade_rows:
                        collection_sale_count += len(trade_rows)
                        raw_events = [
                            _trade_raw_event(
                                trade=trade,
                                collection_name=collection_name,
                                floor_price=floor_price,
                                volume_24h=volume_24h,
                                next_cursor=trade_cursor,
                            )
                            for trade in trade_rows
                        ]
                        raw_written, normalized_written, new_written = _persist_raw_events(store, raw_events)
                        raw_event_count += raw_written
                        normalized_event_count += normalized_written
                        new_event_count += new_written

                    if not has_next_trades:
                        break
                    if max_trade_pages_per_collection is not None and trade_pages >= max_trade_pages_per_collection:
                        break
                    # Safety cap: never exceed 500 pages per collection
                    if trade_pages >= 500:
                        break

                if collection_sale_count > 0:
                    collections_with_sales += 1
            except Exception as exc:
                collection_errors.append(
                    {
                        "collection_address": contract_address,
                        "collection_name": collection_name,
                        "error": str(exc),
                    }
                )

        if not has_next_collections:
            break

    return {
        "market": "okx",
        "event_types": ["sale"],
        "chain": chain,
        "platform": platform,
        "start_time": start_time,
        "end_time": end_time,
        "processed_collections": processed_collections,
        "skipped_collections": skipped_collections,
        "collections_with_sales": collections_with_sales,
        "raw_events_written": raw_event_count,
        "normalized_events_written": normalized_event_count,
        "new_normalized_events": new_event_count,
        "errors": collection_errors,
    }


def backfill_okx_actions_history(
    *,
    client: OKXMarketplaceClient,
    store: SQLiteStore,
    chain: str,
    platform: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    collection_page_limit: int = 300,
    trade_page_limit: int = 50,
    max_collections: int | None = None,
    max_trade_pages_per_collection: int | None = None,
) -> dict[str, Any]:
    return backfill_okx_sales_history(
        client=client,
        store=store,
        chain=chain,
        platform=platform,
        start_time=start_time,
        end_time=end_time,
        collection_page_limit=collection_page_limit,
        trade_page_limit=trade_page_limit,
        max_collections=max_collections,
        max_trade_pages_per_collection=max_trade_pages_per_collection,
    )


def backfill_opensea_actions_history(
    *,
    client: OpenSeaClient,
    store: SQLiteStore,
    slug: str,
    event_types: list[str],
    limit: int = 50,
    max_pages_per_type: int | None = None,
) -> dict[str, Any]:
    collection_payload = client.get_collection(slug=slug)
    stats_payload = client.get_collection_stats(slug=slug)
    collection_name = _first_str((collection_payload.get("name"), collection_payload.get("collection", {}).get("name") if isinstance(collection_payload.get("collection"), dict) else None)) or slug
    floor_price = _to_float(stats_payload.get("floor_price"))
    total_stats = stats_payload.get("total") if isinstance(stats_payload.get("total"), dict) else {}
    volume_24h = _to_float(total_stats.get("one_day") or stats_payload.get("one_day_volume"))

    raw_event_count = 0
    normalized_event_count = 0
    new_event_count = 0
    processed_pages = 0
    errors: list[dict[str, str]] = []

    for event_type in event_types:
        cursor: str | None = None
        pages_for_type = 0
        while True:
            try:
                payload = client.get_collection_events(slug=slug, event_type=event_type, cursor=cursor, limit=limit)
            except Exception as exc:
                errors.append({"event_type": event_type, "cursor": cursor or "", "error": str(exc)})
                break

            records = _opensea_extract_records(payload)
            cursor = _opensea_next_cursor(payload)
            pages_for_type += 1
            processed_pages += 1

            raw_events = [
                _build_opensea_history_event(
                    item,
                    slug=slug,
                    event_type_hint=event_type,
                    collection_name=collection_name,
                    floor_price=floor_price,
                    volume_24h=volume_24h,
                )
                for item in records
            ]
            raw_written, normalized_written, new_written = _persist_raw_events(store, raw_events)
            raw_event_count += raw_written
            normalized_event_count += normalized_written
            new_event_count += new_written

            if cursor is None:
                break
            if max_pages_per_type is not None and pages_for_type >= max_pages_per_type:
                break

    return {
        "market": "opensea",
        "slug": slug,
        "event_types": event_types,
        "processed_pages": processed_pages,
        "raw_events_written": raw_event_count,
        "normalized_events_written": normalized_event_count,
        "new_normalized_events": new_event_count,
        "errors": errors,
    }


def backfill_magiceden_actions_history(
    *,
    client: MagicEdenClient,
    store: SQLiteStore,
    chain: str,
    collection: str,
    types: list[str],
    limit: int = 50,
    max_pages: int | None = None,
) -> dict[str, Any]:
    continuation: str | None = None
    processed_pages = 0
    raw_event_count = 0
    normalized_event_count = 0
    new_event_count = 0

    while True:
        payload = client.get_collection_activity(
            chain=chain,
            collection=collection,
            types=types,
            continuation=continuation,
            limit=limit,
        )
        activities = _magiceden_extract_list(payload, "activities")
        continuation = payload.get("continuation") or None
        processed_pages += 1

        raw_events = [_build_magiceden_history_event(item, collection) for item in activities]
        raw_written, normalized_written, new_written = _persist_raw_events(store, raw_events)
        raw_event_count += raw_written
        normalized_event_count += normalized_written
        new_event_count += new_written

        if continuation is None:
            break
        if max_pages is not None and processed_pages >= max_pages:
            break

    return {
        "market": "magiceden",
        "chain": chain,
        "collection": collection,
        "types": types,
        "processed_pages": processed_pages,
        "raw_events_written": raw_event_count,
        "normalized_events_written": normalized_event_count,
        "new_normalized_events": new_event_count,
    }
