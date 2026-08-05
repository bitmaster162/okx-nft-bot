from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from okx_nft_bot.analytics.portfolio import CollectionPnlSummary, WalletPnlAnalyzer
from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.economics import CollectionEconomics, MassOfferEconomics
from okx_nft_bot.storage.sqlite import SQLiteStore


CurrencyBreakdown = dict[str, float]


@dataclass(slots=True)
class CollectionAllocationRecommendation:
    collection_key: str
    display_name: str
    chain: str
    band: str
    allocation_score: float
    confidence: float
    enabled: bool
    dry_run_only: bool
    preferred_max_total: int | None
    max_total_cap: int | None
    preferred_delay_seconds: float | None
    min_delay_seconds: float | None
    max_existing_offer_cap: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    primary_currency: str | None
    realized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    unrealized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_deployed_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_cost_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_value_by_currency: CurrencyBreakdown = field(default_factory=dict)
    open_position_count: int = 0
    priced_open_position_count: int = 0
    orphan_sale_count: int = 0
    win_rate: float | None = None
    average_hold_hours: float | None = None
    quality_score: float | None = None
    recent_sales_count: int | None = None
    active_offer_count: int | None = None
    latest_trade_at: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[float, float, str]:
        capital = sum(abs(v) for v in self.capital_deployed_by_currency.values())
        return (-self.allocation_score, -capital, self.display_name.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "band": self.band,
            "allocation_score": round(self.allocation_score, 3),
            "confidence": round(self.confidence, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_existing_offer_cap": round(self.max_existing_offer_cap, 6) if self.max_existing_offer_cap is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "primary_currency": self.primary_currency,
            "realized_pnl_by_currency": _round_breakdown(self.realized_pnl_by_currency),
            "unrealized_pnl_by_currency": _round_breakdown(self.unrealized_pnl_by_currency),
            "capital_deployed_by_currency": _round_breakdown(self.capital_deployed_by_currency),
            "inventory_cost_by_currency": _round_breakdown(self.inventory_cost_by_currency),
            "inventory_value_by_currency": _round_breakdown(self.inventory_value_by_currency),
            "open_position_count": self.open_position_count,
            "priced_open_position_count": self.priced_open_position_count,
            "orphan_sale_count": self.orphan_sale_count,
            "win_rate": round(self.win_rate, 3) if self.win_rate is not None else None,
            "average_hold_hours": round(self.average_hold_hours, 3) if self.average_hold_hours is not None else None,
            "quality_score": round(self.quality_score, 3) if self.quality_score is not None else None,
            "recent_sales_count": self.recent_sales_count,
            "active_offer_count": self.active_offer_count,
            "latest_trade_at": self.latest_trade_at,
            "notes": list(self.notes),
        }

    def to_policy_override(self) -> dict[str, Any]:
        payload = {
            "chain": self.chain,
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_existing_offer_cap": round(self.max_existing_offer_cap, 6) if self.max_existing_offer_cap is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "notes": list(self.notes),
            "source": "allocator_report",
            "allocation_band": self.band,
            "allocation_score": round(self.allocation_score, 3),
            "allocation_confidence": round(self.confidence, 4),
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class MassOfferAllocatorReport:
    generated_at: str
    wallet: str
    chain: str
    window_days: int
    policy_path: str
    summary: dict[str, Any]
    collections: list[CollectionAllocationRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
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
            "wallet": self.wallet,
            "chain": self.chain,
            "window_days": self.window_days,
            "summary": self.summary,
            "collections": {item.collection_key: item.to_policy_override() for item in items},
        }


class MassOfferAllocator:
    def __init__(self, *, settings: Settings, store: SQLiteStore | None = None) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.pnl = WalletPnlAnalyzer(settings=settings, store=self.store)
        self.economics = MassOfferEconomics(settings=settings, store=self.store)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
    ) -> MassOfferAllocatorReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_allocator_window_days)
        pnl_report = self.pnl.build_report(
            wallet=wallet,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            collection_limit=None,
            open_limit=None,
            closed_limit=None,
        )
        econ_report = self.economics.build_report(
            chain=resolved_chain,
            window_days=resolved_window_days,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
        )
        econ_map = {item.collection.lower(): item for item in econ_report.collections}
        pnl_map: dict[str, CollectionPnlSummary] = {}
        display_names: dict[str, str] = {}
        for item in pnl_report.collections:
            key = _collection_key(item)
            if not key:
                continue
            pnl_map[key] = item
            display_names[key] = item.collection
        for key, econ in econ_map.items():
            display_names.setdefault(key, key)
            if key not in pnl_map and econ.collection:
                # Keep display name from economics only if wallet has never touched it.
                display_names[key] = key
        all_keys = set(econ_map) | set(pnl_map)
        recommendations: list[CollectionAllocationRecommendation] = []
        for key in sorted(all_keys):
            snapshot = pnl_map.get(key)
            economics = econ_map.get(key)
            recommendation = _build_recommendation(
                settings=self.settings,
                collection_key=key,
                display_name=display_names.get(key) or key,
                chain=resolved_chain,
                snapshot=snapshot,
                economics=economics,
            )
            recommendations.append(recommendation)
        recommendations.sort(key=lambda item: item.sort_key())
        summary = {
            "collection_count": len(recommendations),
            "policy_entries": len(recommendations),
            "overweight_count": sum(1 for item in recommendations if item.band == "overweight"),
            "neutral_count": sum(1 for item in recommendations if item.band == "neutral"),
            "underweight_count": sum(1 for item in recommendations if item.band == "underweight"),
            "watch_count": sum(1 for item in recommendations if item.band == "watch"),
            "block_count": sum(1 for item in recommendations if item.band == "block"),
            "live_enabled_count": sum(1 for item in recommendations if item.enabled and not item.dry_run_only),
            "dry_run_only_count": sum(1 for item in recommendations if item.enabled and item.dry_run_only),
            "top_collection": recommendations[0].collection_key if recommendations else None,
            "top_band": recommendations[0].band if recommendations else None,
            "top_score": round(recommendations[0].allocation_score, 3) if recommendations else 0.0,
        }
        return MassOfferAllocatorReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=pnl_report.wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            policy_path=str(self.settings.mass_offer_allocator_policy_path),
            summary=summary,
            collections=recommendations,
        )

    def write_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        report_path: Path | None = None,
        policy_path: Path | None = None,
        limit: int | None = None,
    ) -> dict[str, str]:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
        )
        resolved_report_path = report_path or self.settings.mass_offer_allocator_report_path
        resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        resolved_policy_path = policy_path or self.settings.mass_offer_allocator_policy_path
        resolved_policy_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_policy_path.write_text(
            json.dumps(report.to_policy_overrides(limit=limit), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "report_path": str(resolved_report_path),
            "policy_path": str(resolved_policy_path),
        }



def format_mass_offer_allocator_text(report: MassOfferAllocatorReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_allocator",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"window_days={report.window_days}",
        f"collections={report.summary.get('collection_count', 0)}",
        f"overweight={report.summary.get('overweight_count', 0)} neutral={report.summary.get('neutral_count', 0)} underweight={report.summary.get('underweight_count', 0)} watch={report.summary.get('watch_count', 0)} block={report.summary.get('block_count', 0)}",
        f"live_enabled={report.summary.get('live_enabled_count', 0)} dry_run_only={report.summary.get('dry_run_only_count', 0)}",
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            f"- {item.display_name} [{item.band}] | score={item.allocation_score:.2f} | conf={item.confidence:.2f} | "
            f"preferred_total={item.preferred_max_total} | preferred_delay={item.preferred_delay_seconds if item.preferred_delay_seconds is not None else 'n/a'} | "
            f"open={item.open_position_count} | realized={_format_breakdown(item.realized_pnl_by_currency, signed=True)} | "
            f"unrealized={_format_breakdown(item.unrealized_pnl_by_currency, signed=True)}"
        )
    return "\n".join(lines)


def _build_recommendation(
    *,
    settings: Settings,
    collection_key: str,
    display_name: str,
    chain: str,
    snapshot: CollectionPnlSummary | None,
    economics: CollectionEconomics | None,
) -> CollectionAllocationRecommendation:
    notes: list[str] = []
    primary_currency = _primary_currency(snapshot)
    realized = _breakdown_value(snapshot.realized_pnl_by_currency, primary_currency) if snapshot and primary_currency else 0.0
    unrealized = _breakdown_value(snapshot.unrealized_pnl_by_currency, primary_currency) if snapshot and primary_currency else 0.0
    deployed = _breakdown_value(snapshot.capital_deployed_by_currency, primary_currency) if snapshot and primary_currency else 0.0
    inventory_cost = _breakdown_value(snapshot.inventory_cost_by_currency, primary_currency) if snapshot and primary_currency else 0.0

    score = 0.0
    if economics is not None:
        score += (float(economics.quality_score) - 50.0) * 0.55
        notes.append(f"economics_quality={economics.quality_score:.1f}")
        if economics.recommended_mode == "aggressive":
            score += 10.0
        elif economics.recommended_mode == "standard":
            score += 4.0
        elif economics.recommended_mode == "probe":
            score -= 5.0
        elif economics.recommended_mode == "throttle":
            score -= 12.0
        elif economics.recommended_mode == "observe":
            score -= 16.0
        if economics.recent_sales_count == 0:
            score -= 6.0
            notes.append("no_recent_sales")
        elif economics.recent_sales_count >= 5:
            score += 6.0
            notes.append("healthy_recent_sales")
        if economics.live_submit_success_rate >= 0.75:
            score += 6.0
            notes.append("strong_submit_conversion")
        elif economics.live_submit_success_rate < 0.35:
            score -= 6.0
            notes.append("weak_submit_conversion")
        if (economics.oldest_active_offer_hours or 0.0) >= 48.0:
            score -= 8.0
            notes.append("stale_active_offers")
    else:
        score -= 5.0
        notes.append("no_economics_snapshot")

    if snapshot is not None:
        if deployed > 0:
            realized_roi_pct = realized / deployed * 100.0
            score += _clamp(realized_roi_pct * 0.9, -28.0, 28.0)
            notes.append(f"realized_roi={realized_roi_pct:.1f}%")
        elif abs(realized) > 1e-12:
            score += _clamp(realized * 15.0, -15.0, 15.0)
            notes.append("realized_without_deployment")
        if inventory_cost > 0 and abs(unrealized) > 1e-12:
            inventory_move_pct = unrealized / inventory_cost * 100.0
            if inventory_move_pct < 0:
                score += _clamp(inventory_move_pct * 0.7, -24.0, 0.0)
            else:
                score += _clamp(inventory_move_pct * 0.35, 0.0, 12.0)
            notes.append(f"inventory_move={inventory_move_pct:.1f}%")
        if snapshot.win_rate is not None:
            score += _clamp((snapshot.win_rate - 50.0) * 0.20, -12.0, 12.0)
            notes.append(f"win_rate={snapshot.win_rate:.1f}%")
        if snapshot.open_position_count > 0:
            score -= min(snapshot.open_position_count * 2.5, 16.0)
            notes.append(f"open_positions={snapshot.open_position_count}")
        if snapshot.orphan_sale_count > 0:
            score -= min(snapshot.orphan_sale_count * 9.0, 27.0)
            notes.append(f"orphan_sales={snapshot.orphan_sale_count}")
        if snapshot.average_hold_hours is not None and snapshot.average_hold_hours > 72.0:
            hold_penalty = min((snapshot.average_hold_hours - 72.0) / 8.0, 12.0)
            score -= hold_penalty
            notes.append(f"slow_turnover={snapshot.average_hold_hours:.1f}h")
        if "unpriced_inventory" in snapshot.notes:
            score -= 12.0
            notes.append("unpriced_inventory")
        if "history_gap_or_untracked_inventory" in snapshot.notes:
            score -= 10.0
            notes.append("history_gap")
    else:
        notes.append("wallet_has_no_trade_history")

    severe_block = False
    if snapshot is not None and snapshot.orphan_sale_count >= 3:
        severe_block = True
        notes.append("severe_history_gap")
    if inventory_cost > 0 and unrealized / inventory_cost <= -0.40:
        severe_block = True
        notes.append("severe_inventory_drawdown")

    score = _clamp(score, -100.0, 100.0)
    confidence = 0.15
    if economics is not None:
        confidence += 0.30
    if snapshot is not None:
        confidence += 0.25
        if snapshot.closed_position_count > 0:
            confidence += 0.15
        if snapshot.open_position_count > 0 or deployed > 0:
            confidence += 0.10
    confidence = _clamp(confidence, 0.10, 0.95)

    if severe_block or score <= -55.0:
        band = "block"
    elif score <= -22.0:
        band = "watch"
    elif score < 8.0:
        band = "underweight"
    elif score < 32.0:
        band = "neutral"
    else:
        band = "overweight"

    base_total = int(economics.recommended_max_total) if economics is not None else min(int(settings.mass_offer_max_total), 6)
    base_delay = float(economics.recommended_delay_seconds) if economics is not None else max(float(settings.mass_offer_delay_seconds), 1.0)
    base_existing_offer_cap = float(economics.recommended_max_existing_offer) if economics is not None and economics.recommended_max_existing_offer is not None else None
    base_active_cap = float(economics.recommended_max_active_exposure_bnb) if economics is not None and economics.recommended_max_active_exposure_bnb is not None else None
    dry_run_from_economics = bool(economics.recommended_dry_run_only) if economics is not None else False
    active_offer_count = int(economics.active_offer_count) if economics is not None else None
    current_exposure = float(economics.active_exposure_bnb) if economics is not None else 0.0

    if band == "overweight":
        enabled = True
        dry_run_only = dry_run_from_economics
        preferred_total = _clamp_int(round(max(base_total, 1) * 1.5), 2, int(settings.mass_offer_max_total))
        preferred_delay = max(base_delay * 0.75, 0.5)
        max_total_cap = preferred_total
        min_delay = preferred_delay
        cap_multiplier = 1.30
    elif band == "neutral":
        enabled = True
        dry_run_only = dry_run_from_economics
        preferred_total = _clamp_int(base_total, 1, int(settings.mass_offer_max_total))
        preferred_delay = max(base_delay, 0.5)
        max_total_cap = preferred_total
        min_delay = preferred_delay
        cap_multiplier = 1.0
    elif band == "underweight":
        enabled = True
        dry_run_only = dry_run_from_economics or score < -5.0
        preferred_total = _clamp_int(round(max(base_total, 1) * 0.6), 1, int(settings.mass_offer_max_total))
        preferred_delay = max(base_delay * 1.35, 1.0)
        max_total_cap = preferred_total
        min_delay = preferred_delay
        cap_multiplier = 0.70
    elif band == "watch":
        enabled = True
        dry_run_only = True
        preferred_total = 1 if (economics is None or (economics.recent_sales_count or 0) == 0) else 2
        preferred_delay = max(base_delay * 2.0, 2.0)
        max_total_cap = preferred_total
        min_delay = preferred_delay
        cap_multiplier = 0.45
    else:
        enabled = False
        dry_run_only = True
        preferred_total = 1
        preferred_delay = max(base_delay * 4.0, 4.0)
        max_total_cap = 1
        min_delay = preferred_delay
        cap_multiplier = 0.25

    if dry_run_only:
        notes.append("allocator_forces_dry_run")
    if not enabled:
        notes.append("allocator_blocks_collection")

    max_active_offers = max(1, min(preferred_total, 5)) if enabled else 1
    max_active_exposure_bnb = _scaled_active_cap(
        base_active_cap=base_active_cap,
        current_exposure=current_exposure,
        multiplier=cap_multiplier,
        preferred_total=preferred_total,
        preferred_price=base_existing_offer_cap,
    )

    notes.extend(
        [
            f"band={band}",
            f"preferred_total={preferred_total}",
            f"preferred_delay={preferred_delay:.2f}s",
        ]
    )

    return CollectionAllocationRecommendation(
        collection_key=collection_key,
        display_name=display_name,
        chain=chain,
        band=band,
        allocation_score=score,
        confidence=confidence,
        enabled=enabled,
        dry_run_only=dry_run_only,
        preferred_max_total=preferred_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=preferred_delay,
        min_delay_seconds=min_delay,
        max_existing_offer_cap=base_existing_offer_cap,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        primary_currency=primary_currency,
        realized_pnl_by_currency=dict(snapshot.realized_pnl_by_currency) if snapshot is not None else {},
        unrealized_pnl_by_currency=dict(snapshot.unrealized_pnl_by_currency) if snapshot is not None else {},
        capital_deployed_by_currency=dict(snapshot.capital_deployed_by_currency) if snapshot is not None else {},
        inventory_cost_by_currency=dict(snapshot.inventory_cost_by_currency) if snapshot is not None else {},
        inventory_value_by_currency=dict(snapshot.inventory_value_by_currency) if snapshot is not None else {},
        open_position_count=int(snapshot.open_position_count) if snapshot is not None else 0,
        priced_open_position_count=int(snapshot.priced_open_position_count) if snapshot is not None else 0,
        orphan_sale_count=int(snapshot.orphan_sale_count) if snapshot is not None else 0,
        win_rate=float(snapshot.win_rate) if snapshot is not None and snapshot.win_rate is not None else None,
        average_hold_hours=float(snapshot.average_hold_hours) if snapshot is not None and snapshot.average_hold_hours is not None else None,
        quality_score=float(economics.quality_score) if economics is not None else None,
        recent_sales_count=int(economics.recent_sales_count) if economics is not None else None,
        active_offer_count=active_offer_count,
        latest_trade_at=(snapshot.latest_trade_at if snapshot is not None else (economics.latest_event_at if economics is not None else None)),
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _collection_key(snapshot: CollectionPnlSummary) -> str:
    if snapshot.contract_address:
        return str(snapshot.contract_address).strip().lower()
    return str(snapshot.collection or "").strip().lower()


def _primary_currency(snapshot: CollectionPnlSummary | None) -> str | None:
    if snapshot is None:
        return None
    weights: dict[str, float] = {}
    for mapping in (
        snapshot.capital_deployed_by_currency,
        snapshot.inventory_cost_by_currency,
        snapshot.inventory_value_by_currency,
        snapshot.realized_pnl_by_currency,
        snapshot.unrealized_pnl_by_currency,
    ):
        for currency, value in mapping.items():
            weights[currency] = weights.get(currency, 0.0) + abs(float(value or 0.0))
    if not weights:
        return None
    return max(sorted(weights), key=lambda item: weights[item])


def _breakdown_value(mapping: Mapping[str, float] | None, currency: str | None) -> float:
    if not mapping or not currency:
        return 0.0
    return float(mapping.get(currency) or 0.0)


def _scaled_active_cap(
    *,
    base_active_cap: float | None,
    current_exposure: float,
    multiplier: float,
    preferred_total: int,
    preferred_price: float | None,
) -> float | None:
    target = None
    if base_active_cap is not None and base_active_cap > 0:
        target = base_active_cap * multiplier
    elif preferred_price is not None and preferred_price > 0:
        target = preferred_price * max(preferred_total, 1) * max(multiplier, 0.25)
    if target is None or target <= 0:
        return None
    if current_exposure > 0:
        target = max(target, current_exposure)
    return round(target, 6)


def _round_breakdown(mapping: Mapping[str, float]) -> dict[str, float]:
    return {str(key): round(float(value), 6) for key, value in sorted(mapping.items())}


def _format_breakdown(mapping: Mapping[str, float], *, signed: bool) -> str:
    if not mapping:
        return "n/a"
    parts: list[str] = []
    for currency, value in sorted(mapping.items()):
        numeric = float(value)
        if signed:
            parts.append(f"{numeric:+.6f} {currency}")
        else:
            parts.append(f"{numeric:.6f} {currency}")
    return ", ".join(parts)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))
