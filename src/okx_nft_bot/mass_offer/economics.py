from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.policy import MassOfferPolicyRegistry
from okx_nft_bot.mass_offer.tracker import MassOfferTracker
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


@dataclass(slots=True)
class CollectionEconomics:
    collection: str
    chain: str
    campaigns_total: int
    campaigns_completed: int
    scanned_total: int
    target_total: int
    submitted_total: int
    dry_run_total: int
    skipped_total: int
    failed_total: int
    blocked_total: int
    active_offer_count: int
    active_exposure_bnb: float
    oldest_active_offer_hours: float | None
    recent_market_events: int
    recent_sales_count: int
    recent_listing_count: int
    latest_floor: float | None
    latest_event_at: str | None
    recent_submit_count: int
    recent_submit_bnb: float
    live_submit_success_rate: float
    quality_score: float
    recommended_mode: str
    recommended_dry_run_only: bool
    recommended_max_total: int
    recommended_delay_seconds: float
    recommended_max_existing_offer: float | None
    recommended_max_active_exposure_bnb: float | None = None
    recommended_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "chain": self.chain,
            "campaigns_total": self.campaigns_total,
            "campaigns_completed": self.campaigns_completed,
            "scanned_total": self.scanned_total,
            "target_total": self.target_total,
            "submitted_total": self.submitted_total,
            "dry_run_total": self.dry_run_total,
            "skipped_total": self.skipped_total,
            "failed_total": self.failed_total,
            "blocked_total": self.blocked_total,
            "active_offer_count": self.active_offer_count,
            "active_exposure_bnb": round(self.active_exposure_bnb, 6),
            "oldest_active_offer_hours": round(self.oldest_active_offer_hours, 2) if self.oldest_active_offer_hours is not None else None,
            "recent_market_events": self.recent_market_events,
            "recent_sales_count": self.recent_sales_count,
            "recent_listing_count": self.recent_listing_count,
            "latest_floor": self.latest_floor,
            "latest_event_at": self.latest_event_at,
            "recent_submit_count": self.recent_submit_count,
            "recent_submit_bnb": round(self.recent_submit_bnb, 6),
            "live_submit_success_rate": round(self.live_submit_success_rate, 4),
            "quality_score": round(self.quality_score, 2),
            "recommended_mode": self.recommended_mode,
            "recommended_dry_run_only": self.recommended_dry_run_only,
            "recommended_max_total": self.recommended_max_total,
            "recommended_delay_seconds": round(self.recommended_delay_seconds, 3),
            "recommended_max_existing_offer": self.recommended_max_existing_offer,
            "recommended_max_active_exposure_bnb": self.recommended_max_active_exposure_bnb,
            "recommended_notes": list(self.recommended_notes),
        }

    def to_policy_override(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "enabled": True,
            "dry_run_only": self.recommended_dry_run_only,
            "max_total_cap": self.recommended_max_total,
            "min_delay_seconds": round(self.recommended_delay_seconds, 3),
            "max_existing_offer_cap": self.recommended_max_existing_offer,
            "max_active_offers": max(1, min(self.recommended_max_total, 5)),
            "max_active_exposure_bnb": self.recommended_max_active_exposure_bnb,
            "notes": list(self.recommended_notes),
            "source": "economics_report",
        }


@dataclass(slots=True)
class MassOfferEconomicsReport:
    generated_at: str
    chain: str
    window_days: int
    policy_path: str
    summary: dict[str, Any]
    collections: list[CollectionEconomics]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "chain": self.chain,
            "window_days": self.window_days,
            "policy_path": self.policy_path,
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }

    def to_policy_overrides(self, *, limit: int | None = None) -> dict[str, Any]:
        items = self.collections[:limit] if limit is not None else self.collections
        return {
            "generated_at": self.generated_at,
            "chain": self.chain,
            "window_days": self.window_days,
            "collections": {item.collection: item.to_policy_override() for item in items},
        }


class MassOfferEconomics:
    def __init__(
        self,
        *,
        settings: Settings,
        tracker: MassOfferTracker | None = None,
        state: PositionState | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self.settings = settings
        self.tracker = tracker or MassOfferTracker(settings.execution_db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.store = store or SQLiteStore(settings.db_path)
        self.policy_registry = MassOfferPolicyRegistry(settings.mass_offer_policy_path)

    def build_report(
        self,
        *,
        chain: str = "bsc",
        window_days: int = 30,
        event_limit: int = 10000,
    ) -> MassOfferEconomicsReport:
        chain_key = chain.lower()
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=max(int(window_days), 1))
        campaign_stats = {
            (row["chain"], row["collection"]): row
            for row in self.tracker.get_collection_campaign_stats(chain=chain_key)
        }
        active_stats = {
            (row["chain"], row["collection"]): row
            for row in self.state.get_collection_active_stats(chain=chain_key)
        }
        submit_stats = {
            (row["chain"], row["collection"]): row
            for row in self.state.get_collection_submit_stats(chain=chain_key, since=since)
        }
        market_stats = _aggregate_market_stats(
            self.store.fetch_analysis_events(limit=event_limit),
            since=since,
        )
        all_keys = set(campaign_stats) | set(active_stats) | set(submit_stats) | set(market_stats)
        collections: list[CollectionEconomics] = []
        for key in sorted(all_keys):
            _, collection = key
            campaign = campaign_stats.get(key, {})
            active = active_stats.get(key, {})
            submit = submit_stats.get(key, {})
            market = market_stats.get(key, {})
            attempts = int(submit.get("submitted_count", 0) or 0) + int(submit.get("failed_count", 0) or 0) + int(submit.get("blocked_count", 0) or 0)
            live_submit_success_rate = (
                float(submit.get("submitted_count", 0) or 0) / attempts if attempts else 0.0
            )
            quality_score = _compute_quality_score(
                recent_market_events=int(market.get("recent_market_events", 0) or 0),
                recent_sales_count=int(market.get("recent_sales_count", 0) or 0),
                latest_event_at=_maybe_dt(market.get("latest_event_at")),
                live_submit_success_rate=live_submit_success_rate,
                active_offer_count=int(active.get("active_offer_count", 0) or 0),
                oldest_active_offer_hours=_coerce_optional_float(active.get("oldest_active_offer_hours")),
                window_days=window_days,
            )
            recommendation = _recommend_policy(
                settings=self.settings,
                quality_score=quality_score,
                recent_market_events=int(market.get("recent_market_events", 0) or 0),
                recent_sales_count=int(market.get("recent_sales_count", 0) or 0),
                active_offer_count=int(active.get("active_offer_count", 0) or 0),
                active_exposure_bnb=float(active.get("active_exposure_bnb", 0.0) or 0.0),
                oldest_active_offer_hours=_coerce_optional_float(active.get("oldest_active_offer_hours")),
                latest_floor=_coerce_optional_float(market.get("latest_floor")),
                recent_submit_count=int(submit.get("recent_submit_count", 0) or 0),
                recent_submit_bnb=float(submit.get("recent_submit_bnb", 0.0) or 0.0),
                live_submit_success_rate=live_submit_success_rate,
            )
            collections.append(
                CollectionEconomics(
                    collection=collection,
                    chain=chain_key,
                    campaigns_total=int(campaign.get("campaigns_total", 0) or 0),
                    campaigns_completed=int(campaign.get("campaigns_completed", 0) or 0),
                    scanned_total=int(campaign.get("scanned_total", 0) or 0),
                    target_total=int(campaign.get("target_total", 0) or 0),
                    submitted_total=int(campaign.get("submitted_total", 0) or 0),
                    dry_run_total=int(campaign.get("dry_run_total", 0) or 0),
                    skipped_total=int(campaign.get("skipped_total", 0) or 0),
                    failed_total=int(campaign.get("failed_total", 0) or 0),
                    blocked_total=int(submit.get("blocked_count", 0) or 0),
                    active_offer_count=int(active.get("active_offer_count", 0) or 0),
                    active_exposure_bnb=float(active.get("active_exposure_bnb", 0.0) or 0.0),
                    oldest_active_offer_hours=_coerce_optional_float(active.get("oldest_active_offer_hours")),
                    recent_market_events=int(market.get("recent_market_events", 0) or 0),
                    recent_sales_count=int(market.get("recent_sales_count", 0) or 0),
                    recent_listing_count=int(market.get("recent_listing_count", 0) or 0),
                    latest_floor=_coerce_optional_float(market.get("latest_floor")),
                    latest_event_at=market.get("latest_event_at"),
                    recent_submit_count=int(submit.get("recent_submit_count", 0) or 0),
                    recent_submit_bnb=float(submit.get("recent_submit_bnb", 0.0) or 0.0),
                    live_submit_success_rate=live_submit_success_rate,
                    quality_score=quality_score,
                    recommended_mode=recommendation["mode"],
                    recommended_dry_run_only=bool(recommendation["dry_run_only"]),
                    recommended_max_total=int(recommendation["max_total"]),
                    recommended_delay_seconds=float(recommendation["delay_seconds"]),
                    recommended_max_existing_offer=_coerce_optional_float(recommendation.get("max_existing_offer")),
                    recommended_max_active_exposure_bnb=_coerce_optional_float(recommendation.get("max_active_exposure_bnb")),
                    recommended_notes=tuple(recommendation["notes"]),
                )
            )
        collections.sort(
            key=lambda item: (
                -item.quality_score,
                -item.recent_sales_count,
                -item.recent_market_events,
                item.collection,
            )
        )
        summary = {
            "collection_count": len(collections),
            "active_offer_count": sum(item.active_offer_count for item in collections),
            "active_exposure_bnb": round(sum(item.active_exposure_bnb for item in collections), 6),
            "dry_run_only_count": sum(1 for item in collections if item.recommended_dry_run_only),
            "aggressive_count": sum(1 for item in collections if item.recommended_mode == "aggressive"),
            "stale_count": sum(1 for item in collections if (item.oldest_active_offer_hours or 0.0) >= 24.0),
            "collections_with_exposure_caps": sum(1 for item in collections if item.recommended_max_active_exposure_bnb is not None),
            "policy_entries": self.policy_registry.count(),
            "top_collection": collections[0].collection if collections else None,
            "top_score": round(collections[0].quality_score, 2) if collections else 0.0,
        }
        return MassOfferEconomicsReport(
            generated_at=now.isoformat(),
            chain=chain_key,
            window_days=max(int(window_days), 1),
            policy_path=str(self.settings.mass_offer_policy_path),
            summary=summary,
            collections=collections,
        )

    def write_report(
        self,
        *,
        chain: str = "bsc",
        window_days: int = 30,
        event_limit: int = 10000,
        report_path: Path | None = None,
        policy_path: Path | None = None,
        limit: int | None = None,
    ) -> dict[str, str]:
        report = self.build_report(chain=chain, window_days=window_days, event_limit=event_limit)
        resolved_report_path = report_path or self.settings.mass_offer_economics_report_path
        resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        resolved_policy_path = policy_path or self.settings.mass_offer_policy_path
        resolved_policy_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_policy_path.write_text(
            json.dumps(report.to_policy_overrides(limit=limit), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "report_path": str(resolved_report_path),
            "policy_path": str(resolved_policy_path),
        }


def format_mass_offer_economics_text(report: MassOfferEconomicsReport, *, limit: int = 5) -> str:
    if not report.collections:
        return (
            "mass_offer_economics\n"
            f"chain={report.chain}\n"
            f"window_days={report.window_days}\n"
            "collections=0"
        )
    lines = [
        "mass_offer_economics",
        f"chain={report.chain}",
        f"window_days={report.window_days}",
        f"collections={report.summary.get('collection_count', 0)}",
        f"active_offers={report.summary.get('active_offer_count', 0)}",
        f"active_exposure_bnb={report.summary.get('active_exposure_bnb', 0.0)}",
        f"dry_run_only={report.summary.get('dry_run_only_count', 0)}",
        f"collections_with_exposure_caps={report.summary.get('collections_with_exposure_caps', 0)}",
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            f"- {item.collection} | mode={item.recommended_mode} | score={item.quality_score:.2f} | "
            f"active={item.active_offer_count} | sales={item.recent_sales_count} | "
            f"max_total={item.recommended_max_total} | delay={item.recommended_delay_seconds:.2f} | "
            f"cap={item.recommended_max_active_exposure_bnb if item.recommended_max_active_exposure_bnb is not None else 'n/a'}"
        )
    return "\n".join(lines)


def _aggregate_market_stats(rows: list[dict[str, Any]], *, since: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        contract = str(row.get("contract_address") or "").strip().lower()
        if not contract:
            continue
        market = str(row.get("market") or "").strip().lower()
        chain = "bsc" if market in {"okx", "magiceden", "binance"} else "bsc"
        event_time = _maybe_dt(row.get("event_time"))
        if event_time is None or event_time < since:
            continue
        bucket = stats.setdefault((chain, contract), {
            "recent_market_events": 0,
            "recent_sales_count": 0,
            "recent_listing_count": 0,
            "latest_floor": None,
            "latest_event_at": None,
        })
        bucket["recent_market_events"] += 1
        event_type = str(row.get("event_type") or "").strip().lower()
        if event_type == "sale":
            bucket["recent_sales_count"] += 1
        if event_type == "listing":
            bucket["recent_listing_count"] += 1
        floor_price = _coerce_optional_float(row.get("floor_price"))
        if floor_price is not None:
            latest_floor = bucket.get("latest_floor")
            if latest_floor is None or event_time.isoformat() >= str(bucket.get("latest_event_at") or ""):
                bucket["latest_floor"] = floor_price
        latest_event_at = bucket.get("latest_event_at")
        if latest_event_at is None or event_time.isoformat() > str(latest_event_at):
            bucket["latest_event_at"] = event_time.isoformat()
    return stats


def _compute_quality_score(
    *,
    recent_market_events: int,
    recent_sales_count: int,
    latest_event_at: datetime | None,
    live_submit_success_rate: float,
    active_offer_count: int,
    oldest_active_offer_hours: float | None,
    window_days: int,
) -> float:
    activity_score = min(recent_market_events / 25.0, 1.0)
    sales_score = min(recent_sales_count / 8.0, 1.0)
    freshness_score = 0.0
    if latest_event_at is not None:
        freshness_hours = max((datetime.now(timezone.utc) - latest_event_at).total_seconds() / 3600.0, 0.0)
        freshness_score = max(0.0, 1.0 - min(freshness_hours / max(window_days * 24.0, 1.0), 1.0))
    execution_score = min(max(live_submit_success_rate, 0.0), 1.0)
    pressure_penalty = min(active_offer_count / 10.0, 1.0)
    staleness_penalty = min((oldest_active_offer_hours or 0.0) / 72.0, 1.0)
    score = 100.0 * (
        0.32 * activity_score
        + 0.28 * sales_score
        + 0.20 * freshness_score
        + 0.20 * execution_score
    )
    score -= 12.0 * pressure_penalty
    score -= 18.0 * staleness_penalty
    return max(min(score, 100.0), 0.0)


def _recommend_policy(
    *,
    settings: Settings,
    quality_score: float,
    recent_market_events: int,
    recent_sales_count: int,
    active_offer_count: int,
    active_exposure_bnb: float,
    oldest_active_offer_hours: float | None,
    latest_floor: float | None,
    recent_submit_count: int,
    recent_submit_bnb: float,
    live_submit_success_rate: float,
) -> dict[str, Any]:
    base_delay = max(float(settings.mass_offer_delay_seconds), 0.1)
    base_total = max(int(settings.mass_offer_max_total), 1)
    notes: list[str] = []

    if recent_market_events == 0 and active_offer_count == 0:
        mode = "observe"
        dry_run_only = True
        max_total = 1
        delay_seconds = base_delay * 3.0
        notes.append("no_recent_market_events")
    elif active_offer_count >= 5 and (oldest_active_offer_hours or 0.0) >= 24.0:
        mode = "throttle"
        dry_run_only = False
        max_total = min(base_total, max(1, 6 - min(active_offer_count, 5)))
        delay_seconds = base_delay * 2.0
        notes.append("stale_active_offer_inventory")
    elif recent_sales_count >= 5 and quality_score >= 70.0 and live_submit_success_rate >= 0.5:
        mode = "aggressive"
        dry_run_only = False
        max_total = min(base_total, 25)
        delay_seconds = max(base_delay * 0.75, 0.5)
        notes.append("strong_recent_sales_and_execution")
    elif recent_sales_count >= 2 and quality_score >= 45.0:
        mode = "standard"
        dry_run_only = False
        max_total = min(base_total, 12)
        delay_seconds = base_delay
        notes.append("healthy_but_not_top_tier")
    else:
        mode = "probe"
        dry_run_only = recent_sales_count == 0 or live_submit_success_rate < 0.35
        max_total = min(base_total, 4)
        delay_seconds = base_delay * 1.5
        if recent_sales_count == 0:
            notes.append("no_recent_sales")
        if live_submit_success_rate < 0.35:
            notes.append("weak_live_submit_conversion")

    if quality_score < 20.0:
        dry_run_only = True
        notes.append("low_quality_score")
    if latest_floor is not None:
        multiplier = {
            "aggressive": 0.90,
            "standard": 0.82,
            "probe": 0.72,
            "throttle": 0.65,
            "observe": 0.55,
        }[mode]
        max_existing_offer = round(latest_floor * multiplier, 6)
        notes.append(f"floor_anchored_cap={max_existing_offer}")
    else:
        max_existing_offer = None
        notes.append("no_floor_snapshot_available")

    max_active_exposure_bnb = _recommend_exposure_cap(
        settings=settings,
        mode=mode,
        max_total=max_total,
        active_offer_count=active_offer_count,
        active_exposure_bnb=active_exposure_bnb,
        recent_submit_count=recent_submit_count,
        recent_submit_bnb=recent_submit_bnb,
        latest_floor=latest_floor,
    )
    if max_active_exposure_bnb is not None:
        notes.append(f"capital_cap={max_active_exposure_bnb}")

    return {
        "mode": mode,
        "dry_run_only": dry_run_only,
        "max_total": max_total,
        "delay_seconds": round(delay_seconds, 3),
        "max_existing_offer": max_existing_offer,
        "max_active_exposure_bnb": max_active_exposure_bnb,
        "notes": tuple(dict.fromkeys(notes)),
    }


def _recommend_exposure_cap(
    *,
    settings: Settings,
    mode: str,
    max_total: int,
    active_offer_count: int,
    active_exposure_bnb: float,
    recent_submit_count: int,
    recent_submit_bnb: float,
    latest_floor: float | None,
) -> float | None:
    unit_price: float | None = None
    if recent_submit_count > 0 and recent_submit_bnb > 0:
        unit_price = recent_submit_bnb / recent_submit_count
    elif active_offer_count > 0 and active_exposure_bnb > 0:
        unit_price = active_exposure_bnb / active_offer_count
    elif latest_floor is not None and latest_floor > 0:
        unit_price = latest_floor * 0.65
    if unit_price is None or unit_price <= 0:
        return None
    mode_multiplier = {
        "aggressive": 1.0,
        "standard": 0.85,
        "probe": 0.65,
        "throttle": 0.55,
        "observe": 0.5,
    }.get(mode, 0.75)
    base_cap = unit_price * max(max_total, 1) * mode_multiplier
    portfolio_guard = max(float(settings.max_bnb_per_day) * 0.4, unit_price)
    cap = min(base_cap, portfolio_guard)
    if active_exposure_bnb > 0:
        cap = max(cap, active_exposure_bnb)
    return round(max(cap, unit_price), 6)

def _maybe_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
