from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


RULE_BUCKETS: dict[str, str] = {
    "repeated_undercut_relist_near_floor": "floor_manipulation",
    "floor_drop_from_small_seller_cluster": "floor_manipulation",
    "sharp_floor_move_without_breadth": "floor_manipulation",
    "repeated_cancel_relist_same_asset": "listing_churn",
    "rapid_short_lived_listing_churn": "listing_churn",
    "price_oscillation_same_asset_short_window": "listing_churn",
    "repeated_trades_same_wallet_pair": "wash_like",
    "low_owner_diversity_high_volume": "wash_like",
    "asset_back_and_forth_transfer_pattern": "wash_like",
    "cheap_bait_listing_disappears_too_fast": "bait_pattern",
}

SEVERITY_SCORES: dict[str, float] = {
    "low": 8.0,
    "medium": 14.0,
    "high": 20.0,
    "critical": 25.0,
}

BUCKET_CAP = 25.0


def score_for_severity(severity: str) -> float:
    return SEVERITY_SCORES[severity]


def compute_risk_score(
    *,
    object_type: str,
    object_id: str,
    rule_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    bucket_scores: dict[str, float] = defaultdict(float)
    for hit in rule_hits:
        bucket = RULE_BUCKETS.get(hit["rule_id"], "other")
        bucket_scores[bucket] = min(BUCKET_CAP, bucket_scores[bucket] + float(hit["score_delta"]))

    total_score = round(min(100.0, sum(bucket_scores.values())), 2)
    if total_score >= 75:
        severity = "AVOID"
    elif total_score >= 45:
        severity = "HIGH_RISK"
    elif total_score >= 20:
        severity = "CAUTION"
    else:
        severity = "SAFE"

    if rule_hits:
        confidence = round(sum(float(hit["confidence"]) for hit in rule_hits) / len(rule_hits), 2)
    else:
        confidence = 0.35

    top_rules = [
        {
            "rule_id": hit["rule_id"],
            "severity": hit["severity"],
            "score_delta": hit["score_delta"],
            "explanation": hit["explanation"],
        }
        for hit in sorted(rule_hits, key=lambda item: (item["score_delta"], item["confidence"]), reverse=True)[:5]
    ]

    return {
        "object_type": object_type,
        "object_id": object_id,
        "total_score": total_score,
        "severity": severity,
        "confidence": confidence,
        "component_scores": dict(bucket_scores),
        "top_rules": top_rules,
        "explanation": {
            "bucket_caps": {"default": BUCKET_CAP},
            "rule_count": len(rule_hits),
            "top_rule_ids": [item["rule_id"] for item in top_rules],
        },
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


def recommended_action(severity: str) -> str:
    return {
        "SAFE": "ignore",
        "CAUTION": "watch",
        "HIGH_RISK": "investigate",
        "AVOID": "avoid",
    }[severity]
