from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.analytics.portfolio_risk import PortfolioRiskAnalyzer, PortfolioRiskReport
from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.allocator import (
    CollectionAllocationRecommendation,
    MassOfferAllocator,
    MassOfferAllocatorReport,
)
from okx_nft_bot.mass_offer.feedback import (
    CollectionFeedbackRecommendation,
    MassOfferFeedbackController,
    apply_feedback_to_recommendation,
)
from okx_nft_bot.mass_offer.policy import CollectionMassOfferPolicy, MassOfferPolicyRegistry
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_STATUS_RANK = {
    "ready": 0,
    "dry_run_only": 1,
    "rate_limited": 2,
    "capped_out": 3,
    "risk_blocked": 4,
    "blocked": 5,
}


@dataclass(slots=True)
class MassOfferPlanItem:
    collection_key: str
    display_name: str
    chain: str
    band: str
    status: str
    allocation_score: float
    confidence: float
    priority_score: float
    enabled: bool
    dry_run_only: bool
    live_eligible: bool
    planned_offer_count: int
    planned_exposure_bnb: float
    price_bnb: float
    recommended_delay_seconds: float
    max_existing_offer_cap: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    exposure_headroom_bnb: float | None = None
    blocked_reason: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, float, float, str]:
        return (
            _STATUS_RANK.get(self.status, 99),
            -self.priority_score,
            -self.allocation_score,
            self.display_name.lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "band": self.band,
            "status": self.status,
            "allocation_score": round(self.allocation_score, 3),
            "confidence": round(self.confidence, 4),
            "priority_score": round(self.priority_score, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "live_eligible": self.live_eligible,
            "planned_offer_count": self.planned_offer_count,
            "planned_exposure_bnb": round(self.planned_exposure_bnb, 6),
            "price_bnb": round(self.price_bnb, 6),
            "recommended_delay_seconds": round(self.recommended_delay_seconds, 6),
            "max_existing_offer_cap": round(self.max_existing_offer_cap, 6) if self.max_existing_offer_cap is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "exposure_headroom_bnb": round(self.exposure_headroom_bnb, 6) if self.exposure_headroom_bnb is not None else None,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class MassOfferPlanReport:
    generated_at: str
    wallet: str | None
    chain: str
    price_bnb: float
    window_days: int
    report_path: str
    allocator: MassOfferAllocatorReport
    risk: PortfolioRiskReport
    summary: dict[str, Any]
    collections: list[MassOfferPlanItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "window_days": self.window_days,
            "report_path": self.report_path,
            "allocator": self.allocator.to_dict(),
            "risk": self.risk.to_dict(),
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }


class MassOfferPlanner:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.allocator = MassOfferAllocator(settings=settings, store=self.store)
        self.feedback = MassOfferFeedbackController(settings=settings, store=self.store, state=self.state, allocator=self.allocator)
        self.risk = PortfolioRiskAnalyzer(settings=settings, store=self.store, state=self.state)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        include_budget_policy: bool = True,
        include_rebalance_policy: bool = True,
        include_quarantine_policy: bool = True,
    ) -> MassOfferPlanReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_allocator_window_days)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        allocator_report = self.allocator.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
        )
        feedback_report = self.feedback.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
        )
        feedback_map = {item.collection_key: item for item in feedback_report.collections}
        budget_registry = MassOfferPolicyRegistry(self.settings.mass_offer_budget_policy_path) if include_budget_policy else None
        rebalance_registry = MassOfferPolicyRegistry(self.settings.mass_offer_rebalance_policy_path) if include_rebalance_policy else None
        quarantine_registry = MassOfferPolicyRegistry(self.settings.mass_offer_quarantine_policy_path) if include_quarantine_policy else None
        risk_report = self.risk.build_report(
            wallet=wallet,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            chain=resolved_chain,
        )
        rate_limits = self.state.get_rate_limit_snapshot(
            max_live_offers_per_hour=self.settings.max_live_offers_per_hour,
            max_bnb_per_day=self.settings.max_bnb_per_day,
            submit_cooldown_seconds=self.settings.submit_cooldown_seconds,
            now=datetime.now(timezone.utc),
        )
        collection_stats = {
            str(row.get("collection") or "").strip().lower(): row
            for row in self.state.get_collection_active_stats(chain=resolved_chain)
        }
        live_rate_limited = (
            int(rate_limits.get("hourly_count") or 0) >= int(rate_limits.get("hourly_limit") or self.settings.max_live_offers_per_hour)
            or float(rate_limits.get("daily_bnb") or 0.0) + resolved_price_bnb > float(rate_limits.get("daily_limit_bnb") or self.settings.max_bnb_per_day) + 1e-12
            or int(rate_limits.get("cooldown_remaining_seconds") or 0) > 0
        )
        risk_blocks_live = bool(risk_report.summary.block_live_submits)

        items: list[MassOfferPlanItem] = []
        for recommendation in allocator_report.collections:
            row = collection_stats.get(recommendation.collection_key, {})
            adjusted = apply_feedback_to_recommendation(recommendation, feedback_map.get(recommendation.collection_key))
            if budget_registry is not None:
                adjusted = _apply_policy_overlay_to_recommendation(
                    adjusted,
                    budget_registry.get(collection=recommendation.collection_key, chain=resolved_chain),
                    note_prefix="budget",
                )
            if rebalance_registry is not None:
                adjusted = _apply_policy_overlay_to_recommendation(
                    adjusted,
                    rebalance_registry.get(collection=recommendation.collection_key, chain=resolved_chain),
                    note_prefix="rebalance",
                )
            if quarantine_registry is not None:
                adjusted = _apply_policy_overlay_to_recommendation(
                    adjusted,
                    quarantine_registry.get(collection=recommendation.collection_key, chain=resolved_chain),
                    note_prefix="quarantine",
                )
            items.append(
                _build_plan_item(
                    recommendation=adjusted,
                    collection_row=row,
                    price_bnb=resolved_price_bnb,
                    risk_blocks_live=risk_blocks_live,
                    live_rate_limited=live_rate_limited,
                )
            )
        items.sort(key=lambda item: item.sort_key())
        summary = {
            "collection_count": len(items),
            "ready_count": sum(1 for item in items if item.status == "ready"),
            "dry_run_only_count": sum(1 for item in items if item.status == "dry_run_only"),
            "rate_limited_count": sum(1 for item in items if item.status == "rate_limited"),
            "capped_out_count": sum(1 for item in items if item.status == "capped_out"),
            "risk_blocked_count": sum(1 for item in items if item.status == "risk_blocked"),
            "blocked_count": sum(1 for item in items if item.status == "blocked"),
            "live_eligible_count": sum(1 for item in items if item.live_eligible),
            "feedback_promote_count": feedback_report.summary.get("promote_count", 0),
            "feedback_steady_count": feedback_report.summary.get("steady_count", 0),
            "feedback_throttle_count": feedback_report.summary.get("throttle_count", 0),
            "feedback_watch_count": feedback_report.summary.get("watch_count", 0),
            "feedback_pause_count": feedback_report.summary.get("pause_count", 0),
            "budget_policy_entries": budget_registry.count() if budget_registry is not None else 0,
            "rebalance_policy_entries": rebalance_registry.count() if rebalance_registry is not None else 0,
            "quarantine_policy_entries": quarantine_registry.count() if quarantine_registry is not None else 0,
            "planned_offer_count": sum(item.planned_offer_count for item in items if item.status in {"ready", "dry_run_only"}),
            "planned_exposure_bnb": round(sum(item.planned_exposure_bnb for item in items if item.status in {"ready", "dry_run_only"}), 6),
            "risk_severity": risk_report.summary.severity,
            "risk_blocks_live": risk_blocks_live,
            "rate_limited": live_rate_limited,
            "hourly_submit_count": int(rate_limits.get("hourly_count") or 0),
            "hourly_limit": int(rate_limits.get("hourly_limit") or self.settings.max_live_offers_per_hour),
            "daily_submit_bnb": round(float(rate_limits.get("daily_bnb") or 0.0), 6),
            "daily_limit_bnb": round(float(rate_limits.get("daily_limit_bnb") or self.settings.max_bnb_per_day), 6),
            "cooldown_remaining_seconds": int(rate_limits.get("cooldown_remaining_seconds") or 0),
            "top_ready_collection": next((item.collection_key for item in items if item.status == "ready"), None),
            "top_ready_score": next((round(item.priority_score, 4) for item in items if item.status == "ready"), 0.0),
        }
        return MassOfferPlanReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=allocator_report.wallet,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            window_days=resolved_window_days,
            report_path=str(self.settings.mass_offer_plan_report_path),
            allocator=allocator_report,
            risk=risk_report,
            summary=summary,
            collections=items,
        )

    def write_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        report_path: Path | None = None,
    ) -> str:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_plan_report_path
        resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(resolved_report_path)


def format_mass_offer_plan_text(report: MassOfferPlanReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_plan",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"price_bnb={report.price_bnb:.6f}",
        f"window_days={report.window_days}",
        (
            f"ready={report.summary.get('ready_count', 0)} dry_run_only={report.summary.get('dry_run_only_count', 0)} "
            f"rate_limited={report.summary.get('rate_limited_count', 0)} capped_out={report.summary.get('capped_out_count', 0)} "
            f"risk_blocked={report.summary.get('risk_blocked_count', 0)} blocked={report.summary.get('blocked_count', 0)}"
        ),
        (
            f"risk={report.summary.get('risk_severity')} blocks_live={report.summary.get('risk_blocks_live')} "
            f"planned_offers={report.summary.get('planned_offer_count', 0)} planned_exposure={report.summary.get('planned_exposure_bnb', 0.0):.6f}"
        ),
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.display_name} [{item.status}/{item.band}] | priority={item.priority_score:.2f} | "
                f"offers={item.planned_offer_count} | delay={item.recommended_delay_seconds:.2f}s | "
                f"headroom={_fmt_float(item.exposure_headroom_bnb)} | live={item.live_eligible}"
            )
        )
        if item.blocked_reason:
            lines.append(f"  reason={item.blocked_reason}")
    return "\n".join(lines)


def _build_plan_item(
    *,
    recommendation: CollectionAllocationRecommendation,
    collection_row: dict[str, Any],
    price_bnb: float,
    risk_blocks_live: bool,
    live_rate_limited: bool,
) -> MassOfferPlanItem:
    current_active_offers = int(collection_row.get("active_offer_count") or 0)
    current_active_exposure_bnb = float(collection_row.get("active_exposure_bnb") or 0.0)
    exposure_cap = recommendation.max_active_exposure_bnb
    exposure_headroom_bnb = None
    if exposure_cap is not None:
        exposure_headroom_bnb = round(float(exposure_cap) - current_active_exposure_bnb, 6)

    planned_offer_count = max(int(recommendation.preferred_max_total or recommendation.max_total_cap or 0), 0)
    notes: list[str] = list(recommendation.notes)

    if recommendation.max_active_offers is not None:
        remaining_offer_slots = max(int(recommendation.max_active_offers) - current_active_offers, 0)
        if planned_offer_count > remaining_offer_slots:
            notes.append(f"offer_slots_clamped:{planned_offer_count}->{remaining_offer_slots}")
            planned_offer_count = remaining_offer_slots

    if exposure_headroom_bnb is not None:
        if exposure_headroom_bnb <= 0:
            planned_offer_count = 0
            notes.append("no_exposure_headroom")
        elif price_bnb > 0:
            max_by_headroom = max(int(math.floor(exposure_headroom_bnb / price_bnb + 1e-12)), 0)
            if planned_offer_count > max_by_headroom:
                notes.append(f"headroom_clamped:{planned_offer_count}->{max_by_headroom}")
                planned_offer_count = max_by_headroom

    planned_exposure_bnb = round(max(planned_offer_count, 0) * max(float(price_bnb), 0.0), 6)
    priority_score = float(recommendation.allocation_score) * max(float(recommendation.confidence), 0.05)
    if recommendation.band == "overweight":
        priority_score += 8.0
    elif recommendation.band == "neutral":
        priority_score += 3.0
    elif recommendation.band == "underweight":
        priority_score -= 4.0
    elif recommendation.band == "watch":
        priority_score -= 10.0
    elif recommendation.band == "block":
        priority_score -= 20.0
    priority_score -= min(current_active_offers * 0.75, 4.0)
    if exposure_headroom_bnb is not None and exposure_headroom_bnb > 0:
        priority_score += min(exposure_headroom_bnb / max(price_bnb, 1e-9), 3.0)

    status = "ready"
    blocked_reason = None
    live_eligible = False
    if not recommendation.enabled:
        status = "blocked"
        blocked_reason = "allocator_disabled"
    elif planned_offer_count <= 0:
        status = "capped_out"
        blocked_reason = "no_capacity"
    elif recommendation.dry_run_only:
        status = "dry_run_only"
        blocked_reason = "allocator_forces_dry_run"
    elif risk_blocks_live:
        status = "risk_blocked"
        blocked_reason = "portfolio_risk_blocks_live"
    elif live_rate_limited:
        status = "rate_limited"
        blocked_reason = "global_rate_limit"
    else:
        live_eligible = True

    return MassOfferPlanItem(
        collection_key=recommendation.collection_key,
        display_name=recommendation.display_name,
        chain=recommendation.chain,
        band=recommendation.band,
        status=status,
        allocation_score=float(recommendation.allocation_score),
        confidence=float(recommendation.confidence),
        priority_score=round(priority_score, 4),
        enabled=bool(recommendation.enabled),
        dry_run_only=bool(recommendation.dry_run_only),
        live_eligible=live_eligible,
        planned_offer_count=planned_offer_count,
        planned_exposure_bnb=planned_exposure_bnb,
        price_bnb=float(price_bnb),
        recommended_delay_seconds=float(recommendation.preferred_delay_seconds or recommendation.min_delay_seconds or 0.0),
        max_existing_offer_cap=recommendation.max_existing_offer_cap,
        max_active_offers=recommendation.max_active_offers,
        max_active_exposure_bnb=recommendation.max_active_exposure_bnb,
        current_active_offers=current_active_offers,
        current_active_exposure_bnb=round(current_active_exposure_bnb, 6),
        exposure_headroom_bnb=exposure_headroom_bnb,
        blocked_reason=blocked_reason,
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )



def _apply_policy_overlay_to_recommendation(
    recommendation: CollectionAllocationRecommendation,
    policy: CollectionMassOfferPolicy | None,
    *,
    note_prefix: str,
) -> CollectionAllocationRecommendation:
    if policy is None:
        return recommendation
    enabled = recommendation.enabled and bool(policy.enabled)
    dry_run_only = recommendation.dry_run_only or bool(policy.dry_run_only)
    preferred_max_total = recommendation.preferred_max_total
    if policy.preferred_max_total is not None and policy.preferred_max_total > 0:
        preferred_max_total = (
            int(policy.preferred_max_total)
            if preferred_max_total is None
            else min(int(preferred_max_total), int(policy.preferred_max_total))
        )
    max_total_cap = recommendation.max_total_cap
    if policy.max_total_cap is not None and policy.max_total_cap > 0:
        max_total_cap = (
            int(policy.max_total_cap)
            if max_total_cap is None
            else min(int(max_total_cap), int(policy.max_total_cap))
        )
    preferred_delay_seconds = recommendation.preferred_delay_seconds
    if policy.preferred_delay_seconds is not None and policy.preferred_delay_seconds > 0:
        preferred_delay_seconds = (
            float(policy.preferred_delay_seconds)
            if preferred_delay_seconds is None
            else max(float(preferred_delay_seconds), float(policy.preferred_delay_seconds))
        )
    min_delay_seconds = recommendation.min_delay_seconds
    if policy.min_delay_seconds is not None and policy.min_delay_seconds >= 0:
        min_delay_seconds = (
            float(policy.min_delay_seconds)
            if min_delay_seconds is None
            else max(float(min_delay_seconds), float(policy.min_delay_seconds))
        )
    max_existing_offer_cap = recommendation.max_existing_offer_cap
    if policy.max_existing_offer_cap is not None and policy.max_existing_offer_cap > 0:
        max_existing_offer_cap = (
            float(policy.max_existing_offer_cap)
            if max_existing_offer_cap is None
            else min(float(max_existing_offer_cap), float(policy.max_existing_offer_cap))
        )
    max_active_offers = recommendation.max_active_offers
    if policy.max_active_offers is not None and policy.max_active_offers > 0:
        max_active_offers = (
            int(policy.max_active_offers)
            if max_active_offers is None
            else min(int(max_active_offers), int(policy.max_active_offers))
        )
    max_active_exposure_bnb = recommendation.max_active_exposure_bnb
    if policy.max_active_exposure_bnb is not None and policy.max_active_exposure_bnb > 0:
        max_active_exposure_bnb = (
            float(policy.max_active_exposure_bnb)
            if max_active_exposure_bnb is None
            else min(float(max_active_exposure_bnb), float(policy.max_active_exposure_bnb))
        )
    notes = tuple(
        dict.fromkeys(
            [
                *recommendation.notes,
                *policy.notes,
                f"{note_prefix}_source={policy.source}",
            ]
        )
    )
    return replace(
        recommendation,
        enabled=enabled,
        dry_run_only=dry_run_only,
        preferred_max_total=preferred_max_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=preferred_delay_seconds,
        min_delay_seconds=min_delay_seconds,
        max_existing_offer_cap=max_existing_offer_cap,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        notes=notes,
    )

def _fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.6f}"
