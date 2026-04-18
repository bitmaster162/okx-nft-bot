from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

from okx_nft_bot.fraud.scoring import score_for_severity


FROZEN_RULE_IDS = [
    "repeated_undercut_relist_near_floor",
    "floor_drop_from_small_seller_cluster",
    "sharp_floor_move_without_breadth",
    "repeated_cancel_relist_same_asset",
    "rapid_short_lived_listing_churn",
    "price_oscillation_same_asset_short_window",
    "repeated_trades_same_wallet_pair",
    "low_owner_diversity_high_volume",
    "asset_back_and_forth_transfer_pattern",
    "cheap_bait_listing_disappears_too_fast",
]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _min_positive(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and value > 0]
    return min(clean) if clean else None


def _make_hit(
    *,
    rule_id: str,
    severity: str,
    explanation: str,
    confidence: float,
    evidence_type: str,
    evidence_payload: dict[str, Any],
    source_refs: list[str],
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "score_delta": score_for_severity(severity),
        "triggered_at": _now().isoformat(),
        "window_start": window_start,
        "window_end": window_end,
        "explanation": explanation,
        "evidence_type": evidence_type,
        "evidence_payload": evidence_payload,
        "source_refs": source_refs,
        "confidence": confidence,
    }


def _listing_lifetime_minutes(listing: dict[str, Any]) -> float | None:
    listed_at = _parse_iso(listing.get("listed_at"))
    delisted_at = _parse_iso(listing.get("delisted_at"))
    if not listed_at or not delisted_at:
        return None
    return (delisted_at - listed_at).total_seconds() / 60.0


def _pair_key(seller: str | None, buyer: str | None) -> tuple[str, str] | None:
    if not seller or not buyer:
        return None
    return tuple(sorted((seller, buyer)))


def _asset_back_and_forth_signal(sales: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(sales) < 3:
        return None
    participants: set[str] = set()
    refs: list[str] = []
    for sale in sales:
        if sale.get("seller_entity_id"):
            participants.add(sale["seller_entity_id"])
        if sale.get("buyer_entity_id"):
            participants.add(sale["buyer_entity_id"])
        refs.append(sale["id"])
    if len(participants) != 2:
        return None
    role_pairs = [
        (sale.get("seller_entity_id"), sale.get("buyer_entity_id"))
        for sale in sales
        if sale.get("seller_entity_id") and sale.get("buyer_entity_id")
    ]
    if len(set(role_pairs)) < 2:
        return None
    return {
        "participants": sorted(participants),
        "count": len(sales),
        "source_refs": refs,
        "window_start": sales[0]["sale_timestamp"],
        "window_end": sales[-1]["sale_timestamp"],
    }


def analyze_asset_rules(
    *,
    asset: dict[str, Any],
    listings: list[dict[str, Any]],
    sales: list[dict[str, Any]],
    collection: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    listing_prices = [listing.get("listed_price_native") for listing in listings if listing.get("listed_price_native") is not None]
    floor_price = collection.get("floor_price_native") or _min_positive(listing_prices)
    features = {
        "listing_count": len(listings),
        "sale_count": len(sales),
        "active_listing_count": sum(1 for listing in listings if listing.get("status") == "active"),
    }

    if floor_price and len(listings) >= 3:
        near_floor = [
            listing for listing in listings
            if listing.get("listed_price_native") is not None
            and float(listing["listed_price_native"]) <= float(floor_price) * 1.05
        ]
        if len(near_floor) >= 2:
            hits.append(
                _make_hit(
                    rule_id="repeated_undercut_relist_near_floor",
                    severity="medium",
                    explanation=f"Asset {asset['id']} was relisted near the observed floor {len(near_floor)} times.",
                    confidence=0.71,
                    evidence_type="listing_timeline",
                    evidence_payload={"floor_price_native": floor_price, "listing_ids": [item["id"] for item in near_floor]},
                    source_refs=[item["id"] for item in near_floor],
                    window_start=near_floor[0].get("listed_at"),
                    window_end=near_floor[-1].get("listed_at"),
                )
            )

    if len(listings) >= 3 and sum(1 for listing in listings if listing.get("status") in {"replaced", "filled"}) >= 2:
        hits.append(
            _make_hit(
                rule_id="repeated_cancel_relist_same_asset",
                severity="medium",
                explanation=f"Asset {asset['id']} has repeated close-and-relist behavior in its listing timeline.",
                confidence=0.76,
                evidence_type="listing_timeline",
                evidence_payload={"statuses": [listing.get("status") for listing in listings]},
                source_refs=[listing["id"] for listing in listings],
                window_start=listings[0].get("listed_at"),
                window_end=listings[-1].get("listed_at"),
            )
        )

    short_lived = [listing for listing in listings if (_listing_lifetime_minutes(listing) or 10_000) <= 60]
    if len(short_lived) >= 2:
        hits.append(
            _make_hit(
                rule_id="rapid_short_lived_listing_churn",
                severity="high",
                explanation=f"Asset {asset['id']} produced {len(short_lived)} short-lived listings inside the churn window.",
                confidence=0.82,
                evidence_type="listing_churn",
                evidence_payload={
                    "listing_ids": [listing["id"] for listing in short_lived],
                    "lifetimes_minutes": [_listing_lifetime_minutes(listing) for listing in short_lived],
                },
                source_refs=[listing["id"] for listing in short_lived],
                window_start=short_lived[0].get("listed_at"),
                window_end=short_lived[-1].get("delisted_at") or short_lived[-1].get("listed_at"),
            )
        )

    if len(listing_prices) >= 3 and min(listing_prices) > 0 and (max(listing_prices) / min(listing_prices)) >= 1.15:
        hits.append(
            _make_hit(
                rule_id="price_oscillation_same_asset_short_window",
                severity="medium",
                explanation=f"Asset {asset['id']} shows a fast listing-price oscillation pattern.",
                confidence=0.74,
                evidence_type="price_oscillation",
                evidence_payload={"prices": listing_prices},
                source_refs=[listing["id"] for listing in listings],
                window_start=listings[0].get("listed_at"),
                window_end=listings[-1].get("listed_at"),
            )
        )

    back_and_forth = _asset_back_and_forth_signal(sales)
    if back_and_forth:
        hits.append(
            _make_hit(
                rule_id="asset_back_and_forth_transfer_pattern",
                severity="high",
                explanation=f"Asset {asset['id']} changed hands back and forth inside a two-wallet loop.",
                confidence=0.84,
                evidence_type="sale_cycle",
                evidence_payload=back_and_forth,
                source_refs=back_and_forth["source_refs"],
                window_start=back_and_forth["window_start"],
                window_end=back_and_forth["window_end"],
            )
        )

    if listing_prices and len(short_lived) >= 1:
        baseline = median(listing_prices)
        bait_listing = next(
            (
                listing
                for listing in short_lived
                if listing.get("listed_price_native") is not None
                and float(listing["listed_price_native"]) <= baseline * 0.85
            ),
            None,
        )
        if bait_listing:
            hits.append(
                _make_hit(
                    rule_id="cheap_bait_listing_disappears_too_fast",
                    severity="high",
                    explanation=f"Asset {asset['id']} had a cheap listing that disappeared unusually fast.",
                    confidence=0.79,
                    evidence_type="bait_listing",
                    evidence_payload={
                        "listing_id": bait_listing["id"],
                        "listed_price_native": bait_listing.get("listed_price_native"),
                        "baseline_price": baseline,
                        "lifetime_minutes": _listing_lifetime_minutes(bait_listing),
                    },
                    source_refs=[bait_listing["id"]],
                    window_start=bait_listing.get("listed_at"),
                    window_end=bait_listing.get("delisted_at"),
                )
            )

    return hits, features


def analyze_collection_rules(
    *,
    collection: dict[str, Any],
    assets: list[dict[str, Any]],
    listings: list[dict[str, Any]],
    sales: list[dict[str, Any]],
    floor_snapshots: list[dict[str, Any]],
    asset_rule_hits: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    active_listings = [listing for listing in listings if listing.get("status") == "active" and listing.get("listed_price_native") is not None]
    active_prices = [float(listing["listed_price_native"]) for listing in active_listings]
    pair_counts = Counter(
        key for key in (_pair_key(sale.get("seller_entity_id"), sale.get("buyer_entity_id")) for sale in sales) if key is not None
    )
    participant_set = {
        participant
        for sale in sales
        for participant in (sale.get("seller_entity_id"), sale.get("buyer_entity_id"))
        if participant
    }

    features = {
        "asset_count": len(assets),
        "active_listing_count": len(active_listings),
        "sale_count": len(sales),
        "unique_participants": len(participant_set),
    }

    if active_prices and len(active_prices) >= 3:
        floor_price = min(active_prices)
        median_price = median(active_prices)
        cheapest = sorted(active_listings, key=lambda item: float(item["listed_price_native"]))[:3]
        cheapest_sellers = {item.get("seller_entity_id") for item in cheapest if item.get("seller_entity_id")}
        if floor_price <= median_price * 0.9 and len(cheapest_sellers) <= 2:
            hits.append(
                _make_hit(
                    rule_id="floor_drop_from_small_seller_cluster",
                    severity="high",
                    explanation="The collection floor is being set by a narrow seller cluster at the cheapest edge of the book.",
                    confidence=0.83,
                    evidence_type="floor_cluster",
                    evidence_payload={
                        "floor_price_native": floor_price,
                        "median_price_native": median_price,
                        "cheapest_listing_ids": [item["id"] for item in cheapest],
                        "seller_count": len(cheapest_sellers),
                    },
                    source_refs=[item["id"] for item in cheapest],
                    window_start=cheapest[0].get("listed_at"),
                    window_end=cheapest[-1].get("listed_at"),
                )
            )

    if len(floor_snapshots) >= 3:
        first = next((item for item in floor_snapshots if item.get("floor_price_native") is not None), None)
        last = next((item for item in reversed(floor_snapshots) if item.get("floor_price_native") is not None), None)
        if first and last and float(first["floor_price_native"]) > 0:
            change_ratio = abs(float(last["floor_price_native"]) - float(first["floor_price_native"])) / float(first["floor_price_native"])
            active_sellers = {listing.get("seller_entity_id") for listing in active_listings if listing.get("seller_entity_id")}
            if change_ratio >= 0.15 and len(active_sellers) <= 2:
                hits.append(
                    _make_hit(
                        rule_id="sharp_floor_move_without_breadth",
                        severity="high",
                        explanation="The collection floor moved sharply without broad seller participation.",
                        confidence=0.78,
                        evidence_type="floor_volatility",
                        evidence_payload={
                            "first_floor_price_native": first["floor_price_native"],
                            "last_floor_price_native": last["floor_price_native"],
                            "change_ratio": round(change_ratio, 4),
                            "active_seller_count": len(active_sellers),
                        },
                        source_refs=[first["id"], last["id"]],
                        window_start=first.get("snapshot_ts"),
                        window_end=last.get("snapshot_ts"),
                    )
                )

    if pair_counts:
        top_pair, top_pair_count = pair_counts.most_common(1)[0]
        if top_pair_count >= 2:
            pair_sales = [
                sale["id"]
                for sale in sales
                if _pair_key(sale.get("seller_entity_id"), sale.get("buyer_entity_id")) == top_pair
            ]
            hits.append(
                _make_hit(
                    rule_id="repeated_trades_same_wallet_pair",
                    severity="high",
                    explanation="The same wallet pair accounts for repeated trades inside the collection.",
                    confidence=0.85,
                    evidence_type="pair_repetition",
                    evidence_payload={"wallet_pair": list(top_pair), "trade_count": top_pair_count},
                    source_refs=pair_sales,
                    window_start=sales[0].get("sale_timestamp") if sales else None,
                    window_end=sales[-1].get("sale_timestamp") if sales else None,
                )
            )

    if len(sales) >= 3 and len(participant_set) <= max(2, len(sales) // 2):
        hits.append(
            _make_hit(
                rule_id="low_owner_diversity_high_volume",
                severity="medium",
                explanation="Trade volume is concentrated in a low-diversity participant set.",
                confidence=0.72,
                evidence_type="owner_diversity",
                evidence_payload={"sale_count": len(sales), "unique_participants": len(participant_set)},
                source_refs=[sale["id"] for sale in sales],
                window_start=sales[0].get("sale_timestamp") if sales else None,
                window_end=sales[-1].get("sale_timestamp") if sales else None,
            )
        )

    best_asset_hits: dict[str, dict[str, Any]] = {}
    for asset_id, asset_hits in asset_rule_hits.items():
        for hit in asset_hits:
            existing = best_asset_hits.get(hit["rule_id"])
            if existing is None or hit["score_delta"] > existing["score_delta"]:
                best_asset_hits[hit["rule_id"]] = hit

    for rule_id in (
        "repeated_undercut_relist_near_floor",
        "repeated_cancel_relist_same_asset",
        "rapid_short_lived_listing_churn",
        "price_oscillation_same_asset_short_window",
        "asset_back_and_forth_transfer_pattern",
        "cheap_bait_listing_disappears_too_fast",
    ):
        hit = best_asset_hits.get(rule_id)
        if hit:
            hits.append(hit)

    return hits, features


def analyze_wallet_rules(
    *,
    entity: dict[str, Any],
    listings: list[dict[str, Any]],
    sales: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    wallet_id = entity["id"]
    counterparty_counts = Counter()
    for sale in sales:
        if sale.get("seller_entity_id") == wallet_id and sale.get("buyer_entity_id"):
            counterparty_counts[sale["buyer_entity_id"]] += 1
        if sale.get("buyer_entity_id") == wallet_id and sale.get("seller_entity_id"):
            counterparty_counts[sale["seller_entity_id"]] += 1

    features = {
        "listing_count": len(listings),
        "sale_count": len(sales),
        "counterparty_count": len(counterparty_counts),
        "link_count": len(links),
    }

    if listings:
        listings_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for listing in listings:
            listings_by_asset[listing["asset_id"]].append(listing)

        repeated_assets = [
            asset_id
            for asset_id, asset_listings in listings_by_asset.items()
            if len(asset_listings) >= 3
        ]
        if repeated_assets:
            refs = [listing["id"] for asset_id in repeated_assets for listing in listings_by_asset[asset_id]]
            hits.append(
                _make_hit(
                    rule_id="repeated_cancel_relist_same_asset",
                    severity="medium",
                    explanation="The wallet repeatedly relisted the same assets in short cycles.",
                    confidence=0.73,
                    evidence_type="wallet_listing_churn",
                    evidence_payload={"asset_ids": repeated_assets},
                    source_refs=refs,
                    window_start=listings[0].get("listed_at"),
                    window_end=listings[-1].get("listed_at"),
                )
            )

        short_lived = [listing for listing in listings if (_listing_lifetime_minutes(listing) or 10_000) <= 60]
        if len(short_lived) >= 2:
            hits.append(
                _make_hit(
                    rule_id="rapid_short_lived_listing_churn",
                    severity="high",
                    explanation="The wallet controls multiple short-lived listings, which looks like churn.",
                    confidence=0.8,
                    evidence_type="wallet_churn",
                    evidence_payload={"listing_ids": [listing["id"] for listing in short_lived]},
                    source_refs=[listing["id"] for listing in short_lived],
                    window_start=short_lived[0].get("listed_at"),
                    window_end=short_lived[-1].get("delisted_at") or short_lived[-1].get("listed_at"),
                )
            )

        listing_prices = [listing.get("listed_price_native") for listing in listings if listing.get("listed_price_native") is not None]
        if len(listing_prices) >= 3:
            baseline = median(listing_prices)
            bait_like = [
                listing for listing in listings
                if listing.get("listed_price_native") is not None
                and float(listing["listed_price_native"]) <= baseline * 0.85
                and (_listing_lifetime_minutes(listing) or 10_000) <= 60
            ]
            if bait_like:
                hits.append(
                    _make_hit(
                        rule_id="cheap_bait_listing_disappears_too_fast",
                        severity="high",
                        explanation="The wallet posted unusually cheap listings that disappeared fast.",
                        confidence=0.77,
                        evidence_type="wallet_bait_pattern",
                        evidence_payload={"listing_ids": [listing["id"] for listing in bait_like], "baseline": baseline},
                        source_refs=[listing["id"] for listing in bait_like],
                        window_start=bait_like[0].get("listed_at"),
                        window_end=bait_like[-1].get("delisted_at") or bait_like[-1].get("listed_at"),
                    )
                )

    if counterparty_counts:
        counterparty, count = counterparty_counts.most_common(1)[0]
        if count >= 2:
            refs = [
                sale["id"]
                for sale in sales
                if counterparty in {sale.get("seller_entity_id"), sale.get("buyer_entity_id")}
            ]
            hits.append(
                _make_hit(
                    rule_id="repeated_trades_same_wallet_pair",
                    severity="high",
                    explanation="The wallet repeatedly traded with the same counterparty.",
                    confidence=0.86,
                    evidence_type="wallet_pair_repetition",
                    evidence_payload={"counterparty": counterparty, "trade_count": count},
                    source_refs=refs,
                    window_start=sales[0].get("sale_timestamp") if sales else None,
                    window_end=sales[-1].get("sale_timestamp") if sales else None,
                )
            )

        if len(sales) >= 3 and len(counterparty_counts) <= 2:
            hits.append(
                _make_hit(
                    rule_id="low_owner_diversity_high_volume",
                    severity="medium",
                    explanation="The wallet's trade history is concentrated in very few counterparties.",
                    confidence=0.7,
                    evidence_type="wallet_diversity",
                    evidence_payload={"sale_count": len(sales), "counterparty_count": len(counterparty_counts)},
                    source_refs=[sale["id"] for sale in sales],
                    window_start=sales[0].get("sale_timestamp") if sales else None,
                    window_end=sales[-1].get("sale_timestamp") if sales else None,
                )
            )

    sales_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sale in sales:
        sales_by_asset[sale["asset_id"]].append(sale)
    for asset_sales in sales_by_asset.values():
        signal = _asset_back_and_forth_signal(asset_sales)
        if signal and wallet_id in signal["participants"]:
            hits.append(
                _make_hit(
                    rule_id="asset_back_and_forth_transfer_pattern",
                    severity="high",
                    explanation="The wallet participates in a repeated back-and-forth asset transfer loop.",
                    confidence=0.83,
                    evidence_type="wallet_asset_cycle",
                    evidence_payload=signal,
                    source_refs=signal["source_refs"],
                    window_start=signal["window_start"],
                    window_end=signal["window_end"],
                )
            )
            break

    return hits, features
