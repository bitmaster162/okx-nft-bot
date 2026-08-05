from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.budget_scheduler import (
    CollectionBudgetAllocation,
    MassOfferBudgetReport,
    MassOfferBudgetScheduler,
)
from okx_nft_bot.mass_offer.feedback import (
    CollectionFeedbackRecommendation,
    MassOfferFeedbackController,
    MassOfferFeedbackReport,
)
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


CurrencyBreakdown = dict[str, float]

_BAND_ORDER = {
    "accelerate": 0,
    "maintain": 1,
    "trim": 2,
    "cooldown": 3,
    "stop": 4,
}

_BUDGET_BASE = {
    "boost": 10.0,
    "steady": 4.0,
    "conserve": -4.0,
    "hold": -10.0,
    "freeze": -18.0,
}

_FEEDBACK_BASE = {
    "promote": 14.0,
    "steady": 4.0,
    "throttle": -8.0,
    "watch": -16.0,
    "pause": -28.0,
}

_ALLOCATOR_BASE = {
    "overweight": 8.0,
    "neutral": 2.0,
    "underweight": -6.0,
    "watch": -14.0,
    "block": -24.0,
}


@dataclass(slots=True)
class CollectionBudgetRebalanceRecommendation:
    collection_key: str
    display_name: str
    chain: str
    rebalance_band: str
    rebalance_score: float
    confidence: float
    enabled: bool
    dry_run_only: bool
    live_eligible: bool
    budget_band: str | None
    feedback_band: str | None
    allocation_band: str | None
    primary_currency: str | None
    preferred_max_total: int | None
    max_total_cap: int | None
    preferred_delay_seconds: float | None
    min_delay_seconds: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    base_scheduled_offer_count: int
    rebalance_offer_count: int
    base_budget_bnb: float
    rebalance_budget_bnb: float
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    resulting_active_exposure_cap_bnb: float | None = None
    live_campaigns: int = 0
    submitted_total: int = 0
    target_total: int = 0
    failed_total: int = 0
    blocked_submit_total: int = 0
    no_submit_live_campaigns: int = 0
    target_utilization: float = 0.0
    submit_success_rate: float = 0.0
    failed_ratio: float = 0.0
    blocked_ratio: float = 0.0
    realized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    unrealized_pnl_by_currency: CurrencyBreakdown = field(default_factory=dict)
    capital_deployed_by_currency: CurrencyBreakdown = field(default_factory=dict)
    inventory_cost_by_currency: CurrencyBreakdown = field(default_factory=dict)
    open_position_count: int = 0
    orphan_sale_count: int = 0
    realized_roi_pct: float | None = None
    unrealized_roi_pct: float | None = None
    blocked_reason: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, float, float, str]:
        return (
            _BAND_ORDER.get(self.rebalance_band, 99),
            -float(self.rebalance_budget_bnb),
            -float(self.rebalance_score),
            self.display_name.lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "rebalance_band": self.rebalance_band,
            "rebalance_score": round(self.rebalance_score, 3),
            "confidence": round(self.confidence, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "live_eligible": self.live_eligible,
            "budget_band": self.budget_band,
            "feedback_band": self.feedback_band,
            "allocation_band": self.allocation_band,
            "primary_currency": self.primary_currency,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "base_scheduled_offer_count": self.base_scheduled_offer_count,
            "rebalance_offer_count": self.rebalance_offer_count,
            "base_budget_bnb": round(self.base_budget_bnb, 6),
            "rebalance_budget_bnb": round(self.rebalance_budget_bnb, 6),
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "resulting_active_exposure_cap_bnb": round(self.resulting_active_exposure_cap_bnb, 6) if self.resulting_active_exposure_cap_bnb is not None else None,
            "live_campaigns": self.live_campaigns,
            "submitted_total": self.submitted_total,
            "target_total": self.target_total,
            "failed_total": self.failed_total,
            "blocked_submit_total": self.blocked_submit_total,
            "no_submit_live_campaigns": self.no_submit_live_campaigns,
            "target_utilization": round(self.target_utilization, 4),
            "submit_success_rate": round(self.submit_success_rate, 4),
            "failed_ratio": round(self.failed_ratio, 4),
            "blocked_ratio": round(self.blocked_ratio, 4),
            "realized_pnl_by_currency": _round_breakdown(self.realized_pnl_by_currency),
            "unrealized_pnl_by_currency": _round_breakdown(self.unrealized_pnl_by_currency),
            "capital_deployed_by_currency": _round_breakdown(self.capital_deployed_by_currency),
            "inventory_cost_by_currency": _round_breakdown(self.inventory_cost_by_currency),
            "open_position_count": self.open_position_count,
            "orphan_sale_count": self.orphan_sale_count,
            "realized_roi_pct": round(self.realized_roi_pct, 4) if self.realized_roi_pct is not None else None,
            "unrealized_roi_pct": round(self.unrealized_roi_pct, 4) if self.unrealized_roi_pct is not None else None,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }

    def to_policy_override(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chain": self.chain,
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "source": "rebalance_report",
            "rebalance_band": self.rebalance_band,
            "rebalance_score": round(self.rebalance_score, 3),
            "rebalance_confidence": round(self.confidence, 4),
            "rebalance_budget_bnb": round(self.rebalance_budget_bnb, 6),
            "notes": list(self.notes),
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class MassOfferRebalanceReport:
    generated_at: str
    wallet: str | None
    chain: str
    price_bnb: float
    window_days: int
    report_path: str
    policy_path: str
    feedback: MassOfferFeedbackReport
    budget: MassOfferBudgetReport
    summary: dict[str, Any]
    collections: list[CollectionBudgetRebalanceRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "window_days": self.window_days,
            "report_path": self.report_path,
            "policy_path": self.policy_path,
            "feedback": self.feedback.to_dict(),
            "budget": self.budget.to_dict(),
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }

    def to_policy_overrides(self, *, limit: int | None = None) -> dict[str, Any]:
        items = self.collections[:limit] if limit is not None else self.collections
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": self.price_bnb,
            "window_days": self.window_days,
            "summary": self.summary,
            "collections": {item.collection_key: item.to_policy_override() for item in items},
        }


@dataclass(slots=True)
class MassOfferRebalanceSyncResult:
    generated_at: str
    wallet: str | None
    chain: str
    window_days: int
    report_path: str
    policy_path: str
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "window_days": self.window_days,
            "report_path": self.report_path,
            "policy_path": self.policy_path,
            "summary": self.summary,
        }


class MassOfferBudgetRebalancer:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
        feedback_controller: MassOfferFeedbackController | None = None,
        budget_scheduler: MassOfferBudgetScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.feedback = feedback_controller or MassOfferFeedbackController(settings=settings, store=self.store, state=self.state)
        self.budget = budget_scheduler or MassOfferBudgetScheduler(settings=settings, store=self.store, state=self.state)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
    ) -> MassOfferRebalanceReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_rebalance_window_days)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        feedback_report = self.feedback.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
        )
        budget_report = self.budget.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
            price_bnb=resolved_price_bnb,
        )
        feedback_map = {item.collection_key: item for item in feedback_report.collections}
        allocator_map = {item.collection_key: item for item in feedback_report.allocator.collections}
        budget_map = {item.collection_key: item for item in budget_report.collections}

        all_keys = set(allocator_map) | set(feedback_map) | set(budget_map)
        recommendations: list[CollectionBudgetRebalanceRecommendation] = []
        for key in sorted(all_keys):
            recommendation = _build_rebalance_recommendation(
                settings=self.settings,
                collection_key=key,
                chain=resolved_chain,
                price_bnb=resolved_price_bnb,
                allocator=allocator_map.get(key),
                feedback=feedback_map.get(key),
                budget=budget_map.get(key),
            )
            recommendations.append(recommendation)
        recommendations.sort(key=lambda item: item.sort_key())
        summary = {
            "collection_count": len(recommendations),
            "policy_entries": len(recommendations),
            "accelerate_count": sum(1 for item in recommendations if item.rebalance_band == "accelerate"),
            "maintain_count": sum(1 for item in recommendations if item.rebalance_band == "maintain"),
            "trim_count": sum(1 for item in recommendations if item.rebalance_band == "trim"),
            "cooldown_count": sum(1 for item in recommendations if item.rebalance_band == "cooldown"),
            "stop_count": sum(1 for item in recommendations if item.rebalance_band == "stop"),
            "live_enabled_count": sum(1 for item in recommendations if item.enabled and not item.dry_run_only),
            "dry_run_only_count": sum(1 for item in recommendations if item.enabled and item.dry_run_only),
            "rebalance_total_budget_bnb": round(sum(item.rebalance_budget_bnb for item in recommendations if item.enabled), 6),
            "base_total_budget_bnb": round(sum(item.base_budget_bnb for item in recommendations), 6),
            "top_collection": recommendations[0].collection_key if recommendations else None,
            "top_band": recommendations[0].rebalance_band if recommendations else None,
            "top_budget_bnb": round(recommendations[0].rebalance_budget_bnb, 6) if recommendations else 0.0,
            "top_score": round(recommendations[0].rebalance_score, 3) if recommendations else 0.0,
            "live_campaigns": sum(item.live_campaigns for item in recommendations),
            "submitted_total": sum(item.submitted_total for item in recommendations),
            "failed_total": sum(item.failed_total for item in recommendations),
            "no_submit_live_campaigns": sum(item.no_submit_live_campaigns for item in recommendations),
        }
        return MassOfferRebalanceReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            window_days=resolved_window_days,
            report_path=str(self.settings.mass_offer_rebalance_report_path),
            policy_path=str(self.settings.mass_offer_rebalance_policy_path),
            feedback=feedback_report,
            budget=budget_report,
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
        price_bnb: float | None = None,
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
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_rebalance_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_rebalance_policy_path
        _write_json(resolved_report_path, report.to_dict())
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(report, report_path=resolved_report_path, policy_path=resolved_policy_path)
        return {"report_path": str(resolved_report_path), "policy_path": str(resolved_policy_path)}

    def sync_policy(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        report_path: Path | None = None,
        policy_path: Path | None = None,
        limit: int | None = None,
    ) -> MassOfferRebalanceSyncResult:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_rebalance_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_rebalance_policy_path
        _write_json(resolved_report_path, report.to_dict())
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(report, report_path=resolved_report_path, policy_path=resolved_policy_path)
        return MassOfferRebalanceSyncResult(
            generated_at=report.generated_at,
            wallet=report.wallet,
            chain=report.chain,
            window_days=report.window_days,
            report_path=str(resolved_report_path),
            policy_path=str(resolved_policy_path),
            summary=report.summary,
        )

    def _persist_runtime_summary(self, report: MassOfferRebalanceReport, *, report_path: Path, policy_path: Path) -> None:
        self.state.set_runtime_value("last_mass_offer_rebalance_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_rebalance_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_rebalance_window_days", report.window_days)
        self.state.set_runtime_value("last_mass_offer_rebalance_report_path", str(report_path))
        self.state.set_runtime_value("last_mass_offer_rebalance_policy_path", str(policy_path))
        self.state.set_runtime_value("last_mass_offer_rebalance_policy_entries", report.summary.get("policy_entries", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_accelerate_count", report.summary.get("accelerate_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_maintain_count", report.summary.get("maintain_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_trim_count", report.summary.get("trim_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_cooldown_count", report.summary.get("cooldown_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_stop_count", report.summary.get("stop_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_live_enabled_count", report.summary.get("live_enabled_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_dry_run_only_count", report.summary.get("dry_run_only_count", 0))
        self.state.set_runtime_value("last_mass_offer_rebalance_total_budget_bnb", report.summary.get("rebalance_total_budget_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_rebalance_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_rebalance_top_band", report.summary.get("top_band"))
        self.state.set_runtime_value("last_mass_offer_rebalance_top_budget_bnb", report.summary.get("top_budget_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_rebalance_top_score", report.summary.get("top_score", 0.0))


def get_mass_offer_rebalance_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_rebalance_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_rebalance_chain"),
        "window_days": _coerce_int(runtime.get("last_mass_offer_rebalance_window_days")),
        "report_path": runtime.get("last_mass_offer_rebalance_report_path"),
        "policy_path": runtime.get("last_mass_offer_rebalance_policy_path"),
        "policy_entries": _coerce_int(runtime.get("last_mass_offer_rebalance_policy_entries")),
        "accelerate_count": _coerce_int(runtime.get("last_mass_offer_rebalance_accelerate_count")),
        "maintain_count": _coerce_int(runtime.get("last_mass_offer_rebalance_maintain_count")),
        "trim_count": _coerce_int(runtime.get("last_mass_offer_rebalance_trim_count")),
        "cooldown_count": _coerce_int(runtime.get("last_mass_offer_rebalance_cooldown_count")),
        "stop_count": _coerce_int(runtime.get("last_mass_offer_rebalance_stop_count")),
        "live_enabled_count": _coerce_int(runtime.get("last_mass_offer_rebalance_live_enabled_count")),
        "dry_run_only_count": _coerce_int(runtime.get("last_mass_offer_rebalance_dry_run_only_count")),
        "rebalance_total_budget_bnb": _coerce_float(runtime.get("last_mass_offer_rebalance_total_budget_bnb")),
        "top_collection": runtime.get("last_mass_offer_rebalance_top_collection"),
        "top_band": runtime.get("last_mass_offer_rebalance_top_band"),
        "top_budget_bnb": _coerce_float(runtime.get("last_mass_offer_rebalance_top_budget_bnb")),
        "top_score": _coerce_float(runtime.get("last_mass_offer_rebalance_top_score")),
    }


def format_mass_offer_rebalance_text(report: MassOfferRebalanceReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_rebalance",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"price_bnb={report.price_bnb:.6f}",
        f"window_days={report.window_days}",
        (
            f"accelerate={report.summary.get('accelerate_count', 0)} maintain={report.summary.get('maintain_count', 0)} "
            f"trim={report.summary.get('trim_count', 0)} cooldown={report.summary.get('cooldown_count', 0)} "
            f"stop={report.summary.get('stop_count', 0)}"
        ),
        (
            f"live_enabled={report.summary.get('live_enabled_count', 0)} dry_run_only={report.summary.get('dry_run_only_count', 0)} "
            f"base_budget={float(report.summary.get('base_total_budget_bnb', 0.0)):.6f} "
            f"rebalance_budget={float(report.summary.get('rebalance_total_budget_bnb', 0.0)):.6f}"
        ),
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.display_name} [{item.rebalance_band}] | score={item.rebalance_score:.2f} | "
                f"budget={item.base_budget_bnb:.4f}->{item.rebalance_budget_bnb:.4f} | "
                f"offers={item.base_scheduled_offer_count}->{item.rebalance_offer_count} | "
                f"delay={_fmt_float(item.preferred_delay_seconds)}"
            )
        )
        if item.blocked_reason:
            lines.append(f"  reason={item.blocked_reason}")
    return "\n".join(lines)


def _build_rebalance_recommendation(
    *,
    settings: Settings,
    collection_key: str,
    chain: str,
    price_bnb: float,
    allocator: Any,
    feedback: CollectionFeedbackRecommendation | None,
    budget: CollectionBudgetAllocation | None,
) -> CollectionBudgetRebalanceRecommendation:
    display_name = _display_name(collection_key, allocator=allocator, feedback=feedback, budget=budget)
    primary_currency = getattr(allocator, "primary_currency", None)
    realized = _breakdown_value(getattr(allocator, "realized_pnl_by_currency", None), primary_currency)
    capital = _breakdown_value(getattr(allocator, "capital_deployed_by_currency", None), primary_currency)
    unrealized = _breakdown_value(getattr(allocator, "unrealized_pnl_by_currency", None), primary_currency)
    inventory_cost = _breakdown_value(getattr(allocator, "inventory_cost_by_currency", None), primary_currency)
    realized_roi_pct = (realized / capital * 100.0) if capital > 0 else None
    unrealized_roi_pct = (unrealized / inventory_cost * 100.0) if inventory_cost > 0 else None

    feedback_band = feedback.feedback_band if feedback is not None else None
    allocation_band = getattr(allocator, "band", None)
    budget_band = budget.budget_band if budget is not None else None

    base_scheduled_offer_count = int(budget.scheduled_offer_count if budget is not None else max(int(getattr(feedback, "preferred_max_total", 0) or getattr(feedback, "max_total_cap", 0) or 0), 0))
    base_budget_bnb = float(budget.allocated_budget_bnb if budget is not None else max(base_scheduled_offer_count, 0) * max(price_bnb, 0.0))
    base_delay = (
        float(budget.recommended_delay_seconds)
        if budget is not None
        else float(getattr(feedback, "preferred_delay_seconds", None) or getattr(feedback, "min_delay_seconds", None) or settings.mass_offer_delay_seconds)
    )
    current_active_offers = int(budget.current_active_offers if budget is not None else getattr(feedback, "current_active_offers", 0) or 0)
    current_active_exposure_bnb = float(budget.current_active_exposure_bnb if budget is not None else getattr(feedback, "current_active_exposure_bnb", 0.0) or 0.0)
    base_active_exposure_cap = (
        float(budget.resulting_active_exposure_cap_bnb)
        if budget is not None and budget.resulting_active_exposure_cap_bnb is not None
        else _optional_float(getattr(feedback, "max_active_exposure_bnb", None))
    )
    base_active_offers_cap = int(getattr(feedback, "max_active_offers", None) or 0) or max(current_active_offers + base_scheduled_offer_count, 1)

    live_campaigns = int(getattr(feedback, "live_campaigns", 0) or 0)
    submitted_total = int(getattr(feedback, "submitted_total", 0) or 0)
    target_total = int(getattr(feedback, "target_total", 0) or 0)
    failed_total = int(getattr(feedback, "failed_total", 0) or 0)
    blocked_submit_total = int(getattr(feedback, "blocked_submit_total", 0) or 0)
    no_submit_live_campaigns = int(getattr(feedback, "no_submit_live_campaigns", 0) or 0)
    target_utilization = float(getattr(feedback, "target_utilization", 0.0) or 0.0)
    submit_success_rate = float(getattr(feedback, "submit_success_rate", 0.0) or 0.0)
    failed_ratio = float(getattr(feedback, "failed_ratio", 0.0) or 0.0)
    blocked_ratio = float(getattr(feedback, "blocked_ratio", 0.0) or 0.0)
    open_position_count = int(getattr(allocator, "open_position_count", 0) or 0)
    orphan_sale_count = int(getattr(allocator, "orphan_sale_count", 0) or 0)

    score = 0.0
    notes: list[str] = []
    score += _BUDGET_BASE.get(str(budget_band or "").lower(), 0.0)
    if budget_band:
        notes.append(f"budget_band={budget_band}")
    score += _FEEDBACK_BASE.get(str(feedback_band or "").lower(), 0.0)
    if feedback_band:
        notes.append(f"feedback_band={feedback_band}")
    score += _ALLOCATOR_BASE.get(str(allocation_band or "").lower(), 0.0)
    if allocation_band:
        notes.append(f"allocator_band={allocation_band}")

    if realized_roi_pct is not None:
        if realized_roi_pct >= 20.0:
            score += 18.0
            notes.append(f"strong_realized_roi={realized_roi_pct:.1f}%")
        elif realized_roi_pct >= 5.0:
            score += 8.0
            notes.append(f"healthy_realized_roi={realized_roi_pct:.1f}%")
        elif realized_roi_pct <= -35.0:
            score -= 24.0
            notes.append(f"severe_realized_drawdown={realized_roi_pct:.1f}%")
        elif realized_roi_pct <= -10.0:
            score -= 11.0
            notes.append(f"negative_realized_roi={realized_roi_pct:.1f}%")
        else:
            score += 1.0
    else:
        notes.append("no_realized_roi")

    if unrealized_roi_pct is not None:
        if unrealized_roi_pct >= 12.0:
            score += 6.0
            notes.append(f"supportive_unrealized_roi={unrealized_roi_pct:.1f}%")
        elif unrealized_roi_pct <= -30.0:
            score -= 15.0
            notes.append(f"severe_unrealized_pressure={unrealized_roi_pct:.1f}%")
        elif unrealized_roi_pct <= -12.0:
            score -= 7.0
            notes.append(f"negative_unrealized_roi={unrealized_roi_pct:.1f}%")
    if live_campaigns > 0:
        score += min(live_campaigns * 1.5, 6.0)
    if target_total > 0:
        if target_utilization >= 0.70:
            score += 10.0
            notes.append(f"strong_utilization={target_utilization:.2f}")
        elif target_utilization >= 0.45:
            score += 4.0
            notes.append(f"healthy_utilization={target_utilization:.2f}")
        elif target_utilization < 0.25:
            score -= 12.0
            notes.append(f"weak_utilization={target_utilization:.2f}")
        elif target_utilization < 0.40:
            score -= 5.0
            notes.append(f"soft_utilization={target_utilization:.2f}")
    if submitted_total + blocked_submit_total + failed_total > 0:
        if submit_success_rate >= 0.75:
            score += 10.0
            notes.append(f"strong_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate >= 0.55:
            score += 4.0
            notes.append(f"healthy_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate < 0.35:
            score -= 11.0
            notes.append(f"weak_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate < 0.50:
            score -= 5.0
            notes.append(f"soft_submit_success={submit_success_rate:.2f}")
        if blocked_ratio >= 0.40:
            score -= 10.0
            notes.append(f"high_blocked_ratio={blocked_ratio:.2f}")
        elif blocked_ratio >= 0.20:
            score -= 4.0
            notes.append(f"elevated_blocked_ratio={blocked_ratio:.2f}")
    if failed_ratio >= 0.50:
        score -= 16.0
        notes.append(f"high_failed_ratio={failed_ratio:.2f}")
    elif failed_ratio >= 0.25:
        score -= 8.0
        notes.append(f"elevated_failed_ratio={failed_ratio:.2f}")
    elif live_campaigns >= 2 and failed_ratio <= 0.05:
        score += 3.0
        notes.append("low_failed_ratio")

    if no_submit_live_campaigns >= 3:
        score -= 18.0
        notes.append(f"repeated_no_submit_live_campaigns={no_submit_live_campaigns}")
    elif no_submit_live_campaigns >= 1 and live_campaigns >= 2:
        score -= 7.0
        notes.append(f"empty_live_campaigns={no_submit_live_campaigns}")

    if orphan_sale_count > 0:
        score -= min(orphan_sale_count * 5.0, 16.0)
        notes.append(f"orphan_sales={orphan_sale_count}")
    if open_position_count > 0:
        score -= min(open_position_count * 1.2, 6.0)
        notes.append(f"open_positions={open_position_count}")

    if base_scheduled_offer_count <= 0 or base_budget_bnb <= 0:
        score -= 6.0
        notes.append("no_base_budget_allocation")

    baseline_enabled = bool((budget.enabled if budget is not None else True) and (getattr(feedback, "enabled", True)) and (getattr(allocator, "enabled", True)))
    baseline_dry_run_only = bool((budget.dry_run_only if budget is not None else False) or bool(getattr(feedback, "dry_run_only", False)) or bool(getattr(allocator, "dry_run_only", False)))
    live_eligible = bool((budget.live_eligible if budget is not None else (not baseline_dry_run_only)) and baseline_enabled and not baseline_dry_run_only)

    score = _clamp(score, -100.0, 100.0)
    confidence = 0.20
    if allocator is not None:
        confidence += min(float(getattr(allocator, "confidence", 0.0) or 0.0) * 0.35, 0.30)
    if feedback is not None:
        confidence += min(float(getattr(feedback, "confidence", 0.0) or 0.0) * 0.35, 0.30)
    if live_campaigns > 0:
        confidence += min(live_campaigns * 0.05, 0.15)
    if realized_roi_pct is not None:
        confidence += 0.08
    if target_total >= 3:
        confidence += 0.06
    confidence = _clamp(confidence, 0.20, 0.95)

    severe_stop = (
        not baseline_enabled
        or (realized_roi_pct is not None and realized_roi_pct <= -35.0 and capital >= max(price_bnb, 0.0) * 3.0)
        or failed_ratio >= 0.75
        or no_submit_live_campaigns >= 3
        or str(feedback_band or "").lower() == "pause"
        or str(allocation_band or "").lower() == "block"
    )
    if severe_stop:
        band = "stop"
    elif score >= 20.0 and live_eligible:
        band = "accelerate"
    elif score >= 2.0:
        band = "maintain"
    elif score >= -14.0:
        band = "trim"
    elif score >= -36.0:
        band = "cooldown"
    else:
        band = "stop"

    if band == "accelerate":
        enabled = baseline_enabled
        dry_run_only = baseline_dry_run_only
        preferred_max_total = _clamp_int(round(max(base_scheduled_offer_count, 1) * 1.35) + (1 if submit_success_rate >= 0.80 else 0), 1, int(settings.mass_offer_max_total))
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 0.85, 0.5)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = max(current_active_offers + preferred_max_total, base_active_offers_cap)
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure_cap, current_active_exposure_bnb, 1.25)
        blocked_reason = None
    elif band == "maintain":
        enabled = baseline_enabled
        dry_run_only = baseline_dry_run_only
        preferred_max_total = max(base_scheduled_offer_count, 1)
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay, 0.5)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = max(base_active_offers_cap, current_active_offers + preferred_max_total)
        max_active_exposure_bnb = base_active_exposure_cap
        blocked_reason = None
    elif band == "trim":
        enabled = baseline_enabled
        dry_run_only = baseline_dry_run_only
        preferred_max_total = _clamp_int(round(max(base_scheduled_offer_count, 1) * 0.65), 1, int(settings.mass_offer_max_total))
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 1.35, 1.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = max(current_active_offers + preferred_max_total, 1)
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure_cap, current_active_exposure_bnb, 0.75)
        blocked_reason = None
    elif band == "cooldown":
        enabled = True
        dry_run_only = True
        preferred_max_total = 1 if submitted_total <= 0 else min(2, max(base_scheduled_offer_count, 1))
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 2.0, 2.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure_cap, current_active_exposure_bnb, 0.4)
        blocked_reason = "rebalance_cooldown"
    else:
        enabled = False
        dry_run_only = True
        preferred_max_total = 1
        max_total_cap = 1
        preferred_delay_seconds = max(base_delay * 4.0, 4.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure_cap, current_active_exposure_bnb, 0.25)
        blocked_reason = "rebalance_stop"

    rebalance_offer_count = max(int(preferred_max_total or 0), 0)
    rebalance_budget_bnb = round(rebalance_offer_count * max(price_bnb, 0.0), 6)
    resulting_active_exposure_cap_bnb = max_active_exposure_bnb
    if dry_run_only:
        notes.append("rebalance_forces_dry_run")
    if not enabled:
        notes.append("rebalance_stops_collection")
    notes.extend(
        [
            f"rebalance_band={band}",
            f"rebalance_score={score:.1f}",
            f"rebalance_budget={rebalance_budget_bnb:.6f}",
            f"rebalance_offer_count={rebalance_offer_count}",
        ]
    )

    return CollectionBudgetRebalanceRecommendation(
        collection_key=collection_key,
        display_name=display_name,
        chain=chain,
        rebalance_band=band,
        rebalance_score=score,
        confidence=confidence,
        enabled=enabled,
        dry_run_only=dry_run_only,
        live_eligible=live_eligible and enabled and not dry_run_only,
        budget_band=budget_band,
        feedback_band=feedback_band,
        allocation_band=allocation_band,
        primary_currency=primary_currency,
        preferred_max_total=preferred_max_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=preferred_delay_seconds,
        min_delay_seconds=min_delay_seconds,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        base_scheduled_offer_count=base_scheduled_offer_count,
        rebalance_offer_count=rebalance_offer_count,
        base_budget_bnb=round(base_budget_bnb, 6),
        rebalance_budget_bnb=rebalance_budget_bnb,
        current_active_offers=current_active_offers,
        current_active_exposure_bnb=round(current_active_exposure_bnb, 6),
        resulting_active_exposure_cap_bnb=resulting_active_exposure_cap_bnb,
        live_campaigns=live_campaigns,
        submitted_total=submitted_total,
        target_total=target_total,
        failed_total=failed_total,
        blocked_submit_total=blocked_submit_total,
        no_submit_live_campaigns=no_submit_live_campaigns,
        target_utilization=target_utilization,
        submit_success_rate=submit_success_rate,
        failed_ratio=failed_ratio,
        blocked_ratio=blocked_ratio,
        realized_pnl_by_currency=dict(getattr(allocator, "realized_pnl_by_currency", {}) or {}),
        unrealized_pnl_by_currency=dict(getattr(allocator, "unrealized_pnl_by_currency", {}) or {}),
        capital_deployed_by_currency=dict(getattr(allocator, "capital_deployed_by_currency", {}) or {}),
        inventory_cost_by_currency=dict(getattr(allocator, "inventory_cost_by_currency", {}) or {}),
        open_position_count=open_position_count,
        orphan_sale_count=orphan_sale_count,
        realized_roi_pct=realized_roi_pct,
        unrealized_roi_pct=unrealized_roi_pct,
        blocked_reason=blocked_reason,
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _display_name(
    collection_key: str,
    *,
    allocator: Any,
    feedback: CollectionFeedbackRecommendation | None,
    budget: CollectionBudgetAllocation | None,
) -> str:
    for value in (
        getattr(allocator, "display_name", None),
        getattr(feedback, "display_name", None),
        getattr(budget, "display_name", None),
    ):
        if isinstance(value, str) and value.strip():
            return value
    return collection_key


def _breakdown_value(breakdown: Any, currency: str | None) -> float:
    if not isinstance(breakdown, dict) or not currency:
        return 0.0
    try:
        return float(breakdown.get(currency, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _round_breakdown(breakdown: CurrencyBreakdown) -> dict[str, float]:
    return {str(key): round(float(value), 6) for key, value in breakdown.items()}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(float(value), upper))


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(int(value), upper))


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _scale_exposure_cap(base_cap: float | None, current_exposure: float, multiplier: float) -> float | None:
    target = None
    if base_cap is not None and base_cap > 0:
        target = float(base_cap) * float(multiplier)
    elif current_exposure > 0:
        target = float(current_exposure) * float(multiplier)
    if target is None or target <= 0:
        return None
    return round(max(target, current_exposure), 6)


def _coerce_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt_float(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.2f}s"
