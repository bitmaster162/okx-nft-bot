from __future__ import annotations

from datetime import timezone
from typing import Any

from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.fraud_store import FraudStore, _utcnow_iso
from okx_nft_bot.storage.sqlite import SQLiteStore


def _normalize_wallet(value: str | None) -> str | None:
    if not value:
        return None
    return value.lower()


def _event_iso(event: NFTEvent) -> str:
    if event.event_time.tzinfo is None:
        return event.event_time.replace(tzinfo=timezone.utc).isoformat()
    return event.event_time.astimezone(timezone.utc).isoformat()


def _safe_collection_part(event: NFTEvent) -> str:
    if event.contract_address:
        return event.contract_address.lower()
    return event.collection.strip().lower().replace(" ", "_")


def collection_id_for_event(event: NFTEvent) -> str:
    return f"{event.market}:unknown:{_safe_collection_part(event)}"


def asset_id_for_event(event: NFTEvent) -> str:
    return f"{collection_id_for_event(event)}:{event.token_id}"


def entity_id_for_event(event: NFTEvent, wallet: str) -> str:
    return f"{event.market}:unknown:wallet:{wallet.lower()}"


def _listing_id(event: NFTEvent) -> str:
    return f"{event.market}:listing:{event.event_id}"


def _sale_id(event: NFTEvent) -> str:
    return f"{event.market}:sale:{event.event_id}"


def _entity_record(event: NFTEvent, wallet: str, role: str) -> dict[str, Any]:
    event_iso = _event_iso(event)
    return {
        "id": entity_id_for_event(event, wallet),
        "marketplace": event.market,
        "chain": "unknown",
        "entity_type": "wallet",
        "wallet_address": wallet.lower(),
        "display_name": wallet.lower(),
        "first_seen_at": event_iso,
        "last_seen_at": event_iso,
        "role_flags_json": [role],
    }


def materialize_from_normalized_events(
    *,
    event_store: SQLiteStore,
    fraud_store: FraudStore,
    market: str | None = "okx",
    limit: int | None = None,
) -> dict[str, Any]:
    events = event_store.fetch_normalized_event_models(limit=limit, market=market)
    processed = 0

    with fraud_store.connect() as conn:
        for event in events:
            processed += 1
            event_iso = _event_iso(event)
            collection_id = collection_id_for_event(event)
            asset_id = asset_id_for_event(event)
            now_iso = _utcnow_iso()

            fraud_store.upsert_collection(
                conn,
                {
                    "id": collection_id,
                    "marketplace": event.market,
                    "chain": "unknown",
                    "external_collection_id": event.contract_address or event.collection,
                    "slug": None,
                    "name": event.collection,
                    "contract_address": event.contract_address.lower() if event.contract_address else None,
                    "floor_price_native": event.floor_price,
                    "volume_24h_native": event.volume_24h,
                    "first_seen_at": event_iso,
                    "last_seen_at": event_iso,
                    "updated_at": now_iso,
                },
            )

            fraud_store.upsert_asset(
                conn,
                {
                    "id": asset_id,
                    "marketplace": event.market,
                    "chain": "unknown",
                    "external_asset_id": event.event_id,
                    "collection_id": collection_id,
                    "token_id": event.token_id,
                    "contract_address": event.contract_address.lower() if event.contract_address else None,
                    "current_listing_price_native": event.price if event.event_type == "listing" else None,
                    "last_sale_price_native": event.price if event.event_type == "sale" else None,
                    "listing_status": "listed" if event.event_type == "listing" else None,
                    "first_seen_at": event_iso,
                    "last_seen_at": event_iso,
                    "updated_at": now_iso,
                },
            )

            if event.maker:
                fraud_store.upsert_entity(conn, _entity_record(event, _normalize_wallet(event.maker) or event.maker, "seller"))
            if event.taker:
                fraud_store.upsert_entity(conn, _entity_record(event, _normalize_wallet(event.taker) or event.taker, "buyer"))

            if event.event_type == "listing":
                listing_id = _listing_id(event)
                seller_entity_id = entity_id_for_event(event, event.maker) if event.maker else None
                fraud_store.close_active_listings_for_asset(
                    conn,
                    asset_id=asset_id,
                    closed_at=event_iso,
                    status="replaced",
                    exclude_listing_id=listing_id,
                )
                fraud_store.upsert_listing(
                    conn,
                    {
                        "id": listing_id,
                        "marketplace": event.market,
                        "chain": "unknown",
                        "external_listing_id": event.tx_hash or event.event_id,
                        "source_event_id": event.event_id,
                        "collection_id": collection_id,
                        "asset_id": asset_id,
                        "seller_entity_id": seller_entity_id,
                        "listed_price_native": event.price,
                        "currency_symbol": event.currency,
                        "listed_at": event_iso,
                        "status": "active",
                        "raw_ref_id": event.event_id,
                        "created_at": event_iso,
                        "updated_at": now_iso,
                    },
                )
                floor_price = event.floor_price if event.floor_price is not None else event.price
                if floor_price is not None:
                    fraud_store.insert_floor_snapshot(
                        conn,
                        {
                            "id": f"floor:{event.event_id}",
                            "collection_id": collection_id,
                            "snapshot_ts": event_iso,
                            "floor_price_native": floor_price,
                            "listing_count": None,
                            "sample_size": 1,
                            "raw_ref_id": event.event_id,
                        },
                    )

            if event.event_type == "sale":
                sale_id = _sale_id(event)
                seller_entity_id = entity_id_for_event(event, event.maker) if event.maker else None
                buyer_entity_id = entity_id_for_event(event, event.taker) if event.taker else None
                fraud_store.close_active_listings_for_asset(
                    conn,
                    asset_id=asset_id,
                    closed_at=event_iso,
                    status="filled",
                )
                fraud_store.upsert_sale(
                    conn,
                    {
                        "id": sale_id,
                        "marketplace": event.market,
                        "chain": "unknown",
                        "external_sale_id": event.tx_hash or event.event_id,
                        "tx_hash": event.tx_hash,
                        "source_event_id": event.event_id,
                        "collection_id": collection_id,
                        "asset_id": asset_id,
                        "seller_entity_id": seller_entity_id,
                        "buyer_entity_id": buyer_entity_id,
                        "sale_price_native": event.price,
                        "currency_symbol": event.currency,
                        "sale_timestamp": event_iso,
                        "raw_ref_id": event.event_id,
                        "created_at": event_iso,
                    },
                )
                if event.floor_price is not None:
                    fraud_store.insert_floor_snapshot(
                        conn,
                        {
                            "id": f"floor:{event.event_id}",
                            "collection_id": collection_id,
                            "snapshot_ts": event_iso,
                            "floor_price_native": event.floor_price,
                            "listing_count": None,
                            "sample_size": 1,
                            "raw_ref_id": event.event_id,
                        },
                    )

        fraud_store.rebuild_asset_state(conn)
        fraud_store.rebuild_collection_aggregates(conn)
        fraud_store.rebuild_entity_links(conn)
        fraud_store.rebuild_entity_metrics(conn)
        conn.commit()

    return {
        "market": market or "all",
        "processed_events": processed,
        "table_counts": fraud_store.table_counts(),
    }
