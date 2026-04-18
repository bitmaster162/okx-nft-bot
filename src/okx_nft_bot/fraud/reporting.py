from __future__ import annotations

from typing import Any

from okx_nft_bot.fraud.rules import analyze_asset_rules, analyze_collection_rules, analyze_wallet_rules
from okx_nft_bot.fraud.scoring import compute_risk_score, recommended_action
from okx_nft_bot.storage.fraud_store import FraudStore


def _risk_block(score: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_score": score["total_score"],
        "severity": score["severity"],
        "confidence": score["confidence"],
        "component_scores": score["component_scores"],
        "top_triggered_rules": score["top_rules"],
    }


def _evidence_block(rule_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rule_id": hit["rule_id"],
            "severity": hit["severity"],
            "explanation": hit["explanation"],
            "confidence": hit["confidence"],
            "source_refs": hit["source_refs"],
            "evidence": hit["evidence_payload"],
        }
        for hit in rule_hits
    ]


def build_collection_report(store: FraudStore, identifier: str) -> dict[str, Any]:
    collection = store.resolve_collection(identifier)
    if not collection:
        raise SystemExit(f"Collection not found: {identifier}")

    assets = store.fetch_collection_assets(collection["id"])
    listings = store.fetch_collection_listings(collection["id"])
    sales = store.fetch_collection_sales(collection["id"])
    floor_snapshots = store.fetch_floor_snapshots(collection["id"])

    asset_rule_hits: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        asset_hits, _asset_features = analyze_asset_rules(
            asset=asset,
            listings=[listing for listing in listings if listing["asset_id"] == asset["id"]],
            sales=[sale for sale in sales if sale["asset_id"] == asset["id"]],
            collection=collection,
        )
        asset_rule_hits[asset["id"]] = asset_hits

    rule_hits, features = analyze_collection_rules(
        collection=collection,
        assets=assets,
        listings=listings,
        sales=sales,
        floor_snapshots=floor_snapshots,
        asset_rule_hits=asset_rule_hits,
    )
    score = compute_risk_score(object_type="collection", object_id=collection["id"], rule_hits=rule_hits)
    with store.connect() as conn:
        store.store_analysis(conn, object_type="collection", object_id=collection["id"], rule_hits=rule_hits, risk_score=score)
        conn.commit()

    return {
        "summary": {
            "object_type": "collection",
            "id": collection["id"],
            "name": collection["name"],
            "marketplace": collection["marketplace"],
            "contract_address": collection.get("contract_address"),
            "metrics": {
                "owner_count": collection.get("owner_count"),
                "item_count": collection.get("item_count"),
                "listing_count": collection.get("listing_count"),
                "floor_price_native": collection.get("floor_price_native"),
                "volume_24h_native": collection.get("volume_24h_native"),
                "features": features,
            },
        },
        "risk_assessment": _risk_block(score),
        "triggered_evidence": _evidence_block(rule_hits),
        "recommended_action": recommended_action(score["severity"]),
    }


def build_asset_report(
    store: FraudStore,
    *,
    asset_id: str | None = None,
    collection_identifier: str | None = None,
    token_id: str | None = None,
) -> dict[str, Any]:
    asset = store.resolve_asset(asset_id=asset_id, collection_identifier=collection_identifier, token_id=token_id)
    if not asset:
        raise SystemExit("Asset not found")
    collection = store.resolve_collection(asset["collection_id"])
    if not collection:
        raise SystemExit(f"Collection not found for asset {asset['id']}")
    listings = store.fetch_asset_listings(asset["id"])
    sales = store.fetch_asset_sales(asset["id"])
    rule_hits, features = analyze_asset_rules(asset=asset, listings=listings, sales=sales, collection=collection)
    score = compute_risk_score(object_type="asset", object_id=asset["id"], rule_hits=rule_hits)
    with store.connect() as conn:
        store.store_analysis(conn, object_type="asset", object_id=asset["id"], rule_hits=rule_hits, risk_score=score)
        conn.commit()

    return {
        "summary": {
            "object_type": "asset",
            "id": asset["id"],
            "collection_id": asset["collection_id"],
            "token_id": asset["token_id"],
            "collection_name": collection["name"],
            "metrics": {
                "current_owner_entity_id": asset.get("current_owner_entity_id"),
                "current_listing_price_native": asset.get("current_listing_price_native"),
                "last_sale_price_native": asset.get("last_sale_price_native"),
                "listing_status": asset.get("listing_status"),
                "features": features,
            },
        },
        "risk_assessment": _risk_block(score),
        "triggered_evidence": _evidence_block(rule_hits),
        "recommended_action": recommended_action(score["severity"]),
    }


def build_wallet_report(store: FraudStore, identifier: str) -> dict[str, Any]:
    entity = store.resolve_entity(identifier)
    if not entity:
        raise SystemExit(f"Wallet not found: {identifier}")
    listings = store.fetch_entity_listings(entity["id"])
    sales = store.fetch_entity_sales(entity["id"])
    links = store.fetch_entity_links(entity["id"])
    rule_hits, features = analyze_wallet_rules(entity=entity, listings=listings, sales=sales, links=links)
    score = compute_risk_score(object_type="wallet", object_id=entity["id"], rule_hits=rule_hits)
    with store.connect() as conn:
        store.store_analysis(conn, object_type="wallet", object_id=entity["id"], rule_hits=rule_hits, risk_score=score)
        conn.commit()

    return {
        "summary": {
            "object_type": "wallet",
            "id": entity["id"],
            "wallet_address": entity.get("wallet_address"),
            "display_name": entity.get("display_name"),
            "marketplace": entity.get("marketplace"),
            "metrics": {
                "linked_entities_count": entity.get("linked_entities_count"),
                "suspicious_score": entity.get("suspicious_score"),
                "features": features,
            },
        },
        "risk_assessment": _risk_block(score),
        "triggered_evidence": _evidence_block(rule_hits),
        "recommended_action": recommended_action(score["severity"]),
    }
