from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.circuit_breaker import MassOfferCircuitBreaker, MassOfferCircuitReport
from okx_nft_bot.mass_offer.plan import MassOfferPlanItem, MassOfferPlanReport, MassOfferPlanner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_BAND_ORDER = {"boost": 0, "steady": 1, "conserve": 2, "hold": 3, "freeze": 4}
_ALLOCATOR_MULTIPLIERS = {
    "overweight": 1.25,
    "neutral": 1.0,
    "underweight": 0.7,
    "watch": 0.35,
    "block": 0.0,
}
_FEEDBACK_MULTIPLIERS = {
    "promote": 1.2,
    "steady": 1.0,
    "throttle": 0.65,
    "watch": 0.35,
    "pause": 0.0,
}


@dataclass(slots=True)
class CollectionBudgetAllocation:
    collection_key: str
    display_name: str
    chain: str
    plan_status: str
    budget_band: str
    circuit_severity: str | None
    circuit_issue_code: str | None
    priority_score: float
    allocation_score: float
    confidence: float
    enabled: bool
    dry_run_only: bool
    live_eligible: bool
    base_planned_offer_count: int
    scheduled_offer_count: int
    base_planned_exposure_bnb: float
    allocated_budget_bnb: float
    allocated_budget_share: float
    price_bnb: float
    recommended_delay_seconds: float
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    resulting_active_exposure_cap_bnb: float | None = None
    blocked_reason: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, float, float, str]:
        return (
            _BAND_ORDER.get(self.budget_band, 99),
            -float(self.allocated_budget_bnb),
            -float(self.priority_score),
            self.display_name.lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "plan_status": self.plan_status,
            "budget_band": self.budget_band,
            "circuit_severity": self.circuit_severity,
            "circuit_issue_code": self.circuit_issue_code,
            "priority_score": round(self.priority_score, 4),
            "allocation_score": round(self.allocation_score, 4),
            "confidence": round(self.confidence, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "live_eligible": self.live_eligible,
            "base_planned_offer_count": self.base_planned_offer_count,
            "scheduled_offer_count": self.scheduled_offer_count,
            "base_planned_exposure_bnb": round(self.base_planned_exposure_bnb, 6),
            "allocated_budget_bnb": round(self.allocated_budget_bnb, 6),
            "allocated_budget_share": round(self.allocated_budget_share, 6),
            "price_bnb": round(self.price_bnb, 6),
            "recommended_delay_seconds": round(self.recommended_delay_seconds, 6),
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "resulting_active_exposure_cap_bnb": round(self.resulting_active_exposure_cap_bnb, 6)
            if self.resulting_active_exposure_cap_bnb is not None
            else None,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }

    def to_policy_override(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chain": self.chain,
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "notes": list(self.notes),
            "source": "budget_scheduler",
            "budget_band": self.budget_band,
            "budget_allocated_bnb": round(self.allocated_budget_bnb, 6),
            "budget_share": round(self.allocated_budget_share, 6),
        }
        if self.recommended_delay_seconds > 0:
            payload["min_delay_seconds"] = round(self.recommended_delay_seconds, 6)
            payload["preferred_delay_seconds"] = round(self.recommended_delay_seconds, 6)
        if self.enabled:
            if self.scheduled_offer_count > 0:
                payload["max_total_cap"] = int(self.scheduled_offer_count)
                payload["max_active_offers"] = max(int(self.current_active_offers) + int(self.scheduled_offer_count), 1)
            elif self.dry_run_only and self.base_planned_offer_count > 0:
                payload["max_total_cap"] = int(self.base_planned_offer_count)
            if self.resulting_active_exposure_cap_bnb is not None and self.resulting_active_exposure_cap_bnb > 0:
                payload["max_active_exposure_bnb"] = round(self.resulting_active_exposure_cap_bnb, 6)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class MassOfferBudgetReport:
    generated_at: str
    wallet: str | None
    chain: str
    price_bnb: float
    window_days: int
    report_path: str
    policy_path: str
    summary: dict[str, Any]
    collections: list[CollectionBudgetAllocation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "window_days": self.window_days,
            "report_path": self.report_path,
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


@dataclass(slots=True)
class MassOfferBudgetSyncResult:
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


class MassOfferBudgetScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
        planner: MassOfferPlanner | None = None,
        circuit_breaker: MassOfferCircuitBreaker | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.planner = planner or MassOfferPlanner(settings=settings, store=self.store, state=self.state)
        self.circuit = circuit_breaker or MassOfferCircuitBreaker(settings=settings, state=self.state)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
    ) -> MassOfferBudgetReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_allocator_window_days)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        now = datetime.now(timezone.utc)
        base_plan = self.planner.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
            price_bnb=resolved_price_bnb,
            include_budget_policy=False,
            include_rebalance_policy=False,
        )
        circuit_report = self.circuit.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_hours=self.settings.mass_offer_circuit_window_hours,
        )
        rate_limits = self.state.get_rate_limit_snapshot(
            now=now,
            max_live_offers_per_hour=self.settings.max_live_offers_per_hour,
            max_bnb_per_day=self.settings.max_bnb_per_day,
            submit_cooldown_seconds=self.settings.submit_cooldown_seconds,
        )
        daily_limit_bnb = float(rate_limits.get("daily_limit_bnb") or self.settings.max_bnb_per_day)
        daily_spent_bnb = float(rate_limits.get("daily_bnb") or 0.0)
        daily_remaining_bnb = max(daily_limit_bnb - daily_spent_bnb - float(getattr(self.settings, "mass_offer_budget_reserve_bnb", 0.0) or 0.0), 0.0)
        hourly_limit = int(rate_limits.get("hourly_limit") or self.settings.max_live_offers_per_hour)
        hourly_count = int(rate_limits.get("hourly_count") or 0)
        hourly_remaining_slots = max(hourly_limit - hourly_count, 0)
        cooldown_remaining_seconds = int(rate_limits.get("cooldown_remaining_seconds") or 0)
        immediate_budget_cap_bnb = daily_remaining_bnb
        if resolved_price_bnb > 0:
            immediate_budget_cap_bnb = min(immediate_budget_cap_bnb, hourly_remaining_slots * resolved_price_bnb)
        global_live_block = bool(circuit_report.summary.get("should_block_live")) or bool(base_plan.summary.get("risk_blocks_live")) or bool(base_plan.summary.get("rate_limited"))
        global_block_reason = None
        if bool(circuit_report.summary.get("should_block_live")):
            global_block_reason = f"circuit:{circuit_report.summary.get('issue_code') or 'halt'}"
        elif bool(base_plan.summary.get("risk_blocks_live")):
            global_block_reason = "plan:portfolio_risk"
        elif bool(base_plan.summary.get("rate_limited")):
            global_block_reason = "plan:rate_limited"
        if cooldown_remaining_seconds > 0 and global_block_reason is None:
            global_block_reason = "governor:cooldown"
            global_live_block = True
        if global_live_block:
            immediate_budget_cap_bnb = 0.0
        total_live_slots = 0
        if resolved_price_bnb > 0:
            total_live_slots = max(int(math.floor(immediate_budget_cap_bnb / resolved_price_bnb + 1e-12)), 0)
        circuit_map = {item.collection_key: item for item in circuit_report.collections}
        live_candidates = [
            item
            for item in base_plan.collections
            if item.status == "ready" and item.live_eligible and item.planned_offer_count > 0
        ]
        weight_map = {
            item.collection_key: _budget_weight(
                item,
                circuit_item=circuit_map.get(item.collection_key),
                caution_multiplier=float(self.settings.mass_offer_budget_caution_multiplier),
                global_live_block=global_live_block,
            )
            for item in live_candidates
        }
        slot_caps = {item.collection_key: int(max(item.planned_offer_count, 0)) for item in live_candidates}
        allocated_slots = _allocate_integer_slots(weight_map=weight_map, slot_caps=slot_caps, total_slots=total_live_slots)
        total_allocated_slots = sum(allocated_slots.values())
        allocated_total_bnb = round(total_allocated_slots * max(resolved_price_bnb, 0.0), 6)
        collections: list[CollectionBudgetAllocation] = []
        for item in base_plan.collections:
            circuit_item = circuit_map.get(item.collection_key)
            schedule = _build_collection_schedule(
                item=item,
                resolved_price_bnb=resolved_price_bnb,
                scheduled_offer_count=int(allocated_slots.get(item.collection_key, 0)),
                allocated_total_slots=total_allocated_slots,
                available_live_budget_bnb=immediate_budget_cap_bnb,
                circuit_severity=circuit_item.severity if circuit_item is not None else None,
                circuit_issue_code=circuit_item.issue_code if circuit_item is not None else None,
                global_live_block=global_live_block,
                global_block_reason=global_block_reason,
            )
            collections.append(schedule)
        collections.sort(key=lambda item: item.sort_key())
        summary = {
            "collection_count": len(collections),
            "policy_entries": len(collections),
            "boost_count": sum(1 for item in collections if item.budget_band == "boost"),
            "steady_count": sum(1 for item in collections if item.budget_band == "steady"),
            "conserve_count": sum(1 for item in collections if item.budget_band == "conserve"),
            "hold_count": sum(1 for item in collections if item.budget_band == "hold"),
            "freeze_count": sum(1 for item in collections if item.budget_band == "freeze"),
            "live_candidate_count": len(live_candidates),
            "daily_limit_bnb": round(daily_limit_bnb, 6),
            "daily_spent_bnb": round(daily_spent_bnb, 6),
            "daily_remaining_bnb": round(daily_remaining_bnb, 6),
            "available_live_budget_bnb": round(immediate_budget_cap_bnb, 6),
            "allocated_total_bnb": allocated_total_bnb,
            "unallocated_bnb": round(max(immediate_budget_cap_bnb - allocated_total_bnb, 0.0), 6),
            "allocated_total_slots": total_allocated_slots,
            "hourly_remaining_slots": hourly_remaining_slots,
            "cooldown_remaining_seconds": cooldown_remaining_seconds,
            "global_live_block": global_live_block,
            "global_block_reason": global_block_reason,
            "top_collection": collections[0].collection_key if collections else None,
            "top_band": collections[0].budget_band if collections else None,
            "top_budget_bnb": round(collections[0].allocated_budget_bnb, 6) if collections else 0.0,
            "top_budget_offers": collections[0].scheduled_offer_count if collections else 0,
        }
        return MassOfferBudgetReport(
            generated_at=now.isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            window_days=resolved_window_days,
            report_path=str(self.settings.mass_offer_budget_report_path),
            policy_path=str(self.settings.mass_offer_budget_policy_path),
            summary=summary,
            collections=collections,
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
        resolved_report_path = report_path or self.settings.mass_offer_budget_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_budget_policy_path
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
    ) -> MassOfferBudgetSyncResult:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_budget_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_budget_policy_path
        _write_json(resolved_report_path, report.to_dict())
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(report, report_path=resolved_report_path, policy_path=resolved_policy_path)
        return MassOfferBudgetSyncResult(
            generated_at=report.generated_at,
            wallet=report.wallet,
            chain=report.chain,
            window_days=report.window_days,
            report_path=str(resolved_report_path),
            policy_path=str(resolved_policy_path),
            summary=report.summary,
        )

    def _persist_runtime_summary(self, report: MassOfferBudgetReport, *, report_path: Path, policy_path: Path) -> None:
        self.state.set_runtime_value("last_mass_offer_budget_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_budget_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_budget_window_days", report.window_days)
        self.state.set_runtime_value("last_mass_offer_budget_report_path", str(report_path))
        self.state.set_runtime_value("last_mass_offer_budget_policy_path", str(policy_path))
        self.state.set_runtime_value("last_mass_offer_budget_policy_entries", report.summary.get("policy_entries", 0))
        self.state.set_runtime_value("last_mass_offer_budget_boost_count", report.summary.get("boost_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_steady_count", report.summary.get("steady_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_conserve_count", report.summary.get("conserve_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_hold_count", report.summary.get("hold_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_freeze_count", report.summary.get("freeze_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_live_candidate_count", report.summary.get("live_candidate_count", 0))
        self.state.set_runtime_value("last_mass_offer_budget_available_live_bnb", report.summary.get("available_live_budget_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_budget_allocated_total_bnb", report.summary.get("allocated_total_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_budget_allocated_total_slots", report.summary.get("allocated_total_slots", 0))
        self.state.set_runtime_value("last_mass_offer_budget_daily_remaining_bnb", report.summary.get("daily_remaining_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_budget_hourly_remaining_slots", report.summary.get("hourly_remaining_slots", 0))
        self.state.set_runtime_value("last_mass_offer_budget_global_live_block", "1" if report.summary.get("global_live_block") else "0")
        self.state.set_runtime_value("last_mass_offer_budget_global_block_reason", report.summary.get("global_block_reason"))
        self.state.set_runtime_value("last_mass_offer_budget_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_budget_top_band", report.summary.get("top_band"))
        self.state.set_runtime_value("last_mass_offer_budget_top_budget_bnb", report.summary.get("top_budget_bnb", 0.0))


def get_mass_offer_budget_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_budget_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_budget_chain"),
        "window_days": _coerce_int(runtime.get("last_mass_offer_budget_window_days")),
        "report_path": runtime.get("last_mass_offer_budget_report_path"),
        "policy_path": runtime.get("last_mass_offer_budget_policy_path"),
        "policy_entries": _coerce_int(runtime.get("last_mass_offer_budget_policy_entries")),
        "boost_count": _coerce_int(runtime.get("last_mass_offer_budget_boost_count")),
        "steady_count": _coerce_int(runtime.get("last_mass_offer_budget_steady_count")),
        "conserve_count": _coerce_int(runtime.get("last_mass_offer_budget_conserve_count")),
        "hold_count": _coerce_int(runtime.get("last_mass_offer_budget_hold_count")),
        "freeze_count": _coerce_int(runtime.get("last_mass_offer_budget_freeze_count")),
        "live_candidate_count": _coerce_int(runtime.get("last_mass_offer_budget_live_candidate_count")),
        "available_live_budget_bnb": _coerce_float(runtime.get("last_mass_offer_budget_available_live_bnb")),
        "allocated_total_bnb": _coerce_float(runtime.get("last_mass_offer_budget_allocated_total_bnb")),
        "allocated_total_slots": _coerce_int(runtime.get("last_mass_offer_budget_allocated_total_slots")),
        "daily_remaining_bnb": _coerce_float(runtime.get("last_mass_offer_budget_daily_remaining_bnb")),
        "hourly_remaining_slots": _coerce_int(runtime.get("last_mass_offer_budget_hourly_remaining_slots")),
        "global_live_block": bool(_coerce_optional_bool(runtime.get("last_mass_offer_budget_global_live_block"))),
        "global_block_reason": runtime.get("last_mass_offer_budget_global_block_reason"),
        "top_collection": runtime.get("last_mass_offer_budget_top_collection"),
        "top_band": runtime.get("last_mass_offer_budget_top_band"),
        "top_budget_bnb": _coerce_float(runtime.get("last_mass_offer_budget_top_budget_bnb")),
    }


def format_mass_offer_budget_text(report: MassOfferBudgetReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_budget",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"price_bnb={report.price_bnb:.6f}",
        f"window_days={report.window_days}",
        (
            f"daily_remaining={float(report.summary.get('daily_remaining_bnb', 0.0)):.6f} "
            f"live_budget={float(report.summary.get('available_live_budget_bnb', 0.0)):.6f} "
            f"allocated={float(report.summary.get('allocated_total_bnb', 0.0)):.6f} "
            f"hourly_slots={int(report.summary.get('hourly_remaining_slots', 0) or 0)}"
        ),
        (
            f"boost={report.summary.get('boost_count', 0)} steady={report.summary.get('steady_count', 0)} "
            f"conserve={report.summary.get('conserve_count', 0)} hold={report.summary.get('hold_count', 0)} "
            f"freeze={report.summary.get('freeze_count', 0)} live_block={bool(report.summary.get('global_live_block'))}"
        ),
    ]
    block_reason = report.summary.get("global_block_reason")
    if block_reason:
        lines.append(f"global_block_reason={block_reason}")
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.display_name} [{item.plan_status}/{item.budget_band}] | budget={item.allocated_budget_bnb:.6f} | "
                f"offers={item.scheduled_offer_count}/{item.base_planned_offer_count} | delay={item.recommended_delay_seconds:.2f}s | "
                f"live={item.live_eligible and not item.dry_run_only and item.enabled}"
            )
        )
        if item.blocked_reason:
            lines.append(f"  reason={item.blocked_reason}")
    return "\n".join(lines)


def _build_collection_schedule(
    *,
    item: MassOfferPlanItem,
    resolved_price_bnb: float,
    scheduled_offer_count: int,
    allocated_total_slots: int,
    available_live_budget_bnb: float,
    circuit_severity: str | None,
    circuit_issue_code: str | None,
    global_live_block: bool,
    global_block_reason: str | None,
) -> CollectionBudgetAllocation:
    notes = list(item.notes)
    base_planned_offer_count = max(int(item.planned_offer_count), 0)
    allocated_budget_bnb = round(max(scheduled_offer_count, 0) * max(resolved_price_bnb, 0.0), 6)
    allocated_budget_share = 0.0
    if available_live_budget_bnb > 0:
        allocated_budget_share = min(allocated_budget_bnb / available_live_budget_bnb, 1.0)
    recommended_delay_seconds = max(float(item.recommended_delay_seconds), 0.0)
    enabled = bool(item.enabled)
    dry_run_only = bool(item.dry_run_only)
    blocked_reason = item.blocked_reason
    band = "hold"

    if not enabled or item.status == "blocked":
        band = "freeze"
        enabled = False
        dry_run_only = False
        blocked_reason = blocked_reason or "plan_blocked"
    elif circuit_severity == "halt":
        band = "freeze"
        enabled = False
        dry_run_only = False
        blocked_reason = blocked_reason or f"circuit:{circuit_issue_code or 'halt'}"
        notes.append(f"circuit_severity={circuit_severity}")
    elif item.status in {"risk_blocked", "rate_limited", "dry_run_only", "capped_out"}:
        band = "hold"
        dry_run_only = True
        blocked_reason = blocked_reason or {
            "risk_blocked": "plan_risk_blocked",
            "rate_limited": "plan_rate_limited",
            "dry_run_only": "plan_dry_run_only",
            "capped_out": "plan_no_capacity",
        }.get(item.status, "plan_hold")
        recommended_delay_seconds *= 1.5
    elif item.status == "ready":
        if global_live_block:
            band = "hold"
            dry_run_only = True
            blocked_reason = global_block_reason or "global_live_block"
            recommended_delay_seconds *= 1.5
        elif scheduled_offer_count <= 0:
            band = "hold"
            dry_run_only = True
            blocked_reason = "budget_unallocated"
            recommended_delay_seconds *= 1.35
        else:
            ratio = scheduled_offer_count / max(base_planned_offer_count, 1)
            if ratio >= 0.8:
                band = "boost"
                recommended_delay_seconds *= 0.95
            elif ratio >= 0.45:
                band = "steady"
            else:
                band = "conserve"
                recommended_delay_seconds *= 1.15
            if circuit_severity == "caution":
                band = "conserve" if band != "freeze" else band
                recommended_delay_seconds *= 1.15
                notes.append(f"circuit_severity={circuit_severity}")
    else:
        band = "hold"
        dry_run_only = True
        blocked_reason = blocked_reason or f"plan_status:{item.status}"

    if band == "hold" and not any(note.startswith("budget_band=") for note in notes):
        notes.append("budget_band=hold")
    elif band != "hold":
        notes.append(f"budget_band={band}")
    if scheduled_offer_count > 0:
        notes.append(f"budget_offers={scheduled_offer_count}")
        notes.append(f"budget_allocated_bnb={allocated_budget_bnb:.6f}")
    if blocked_reason:
        notes.append(f"budget_reason={blocked_reason}")

    resulting_active_exposure_cap_bnb = None
    if allocated_budget_bnb > 0:
        resulting_active_exposure_cap_bnb = round(item.current_active_exposure_bnb + allocated_budget_bnb, 6)
    elif item.current_active_exposure_bnb > 0:
        resulting_active_exposure_cap_bnb = round(item.current_active_exposure_bnb, 6)

    return CollectionBudgetAllocation(
        collection_key=item.collection_key,
        display_name=item.display_name,
        chain=item.chain,
        plan_status=item.status,
        budget_band=band,
        circuit_severity=circuit_severity,
        circuit_issue_code=circuit_issue_code,
        priority_score=float(item.priority_score),
        allocation_score=float(item.allocation_score),
        confidence=float(item.confidence),
        enabled=enabled,
        dry_run_only=dry_run_only,
        live_eligible=bool(item.live_eligible),
        base_planned_offer_count=base_planned_offer_count,
        scheduled_offer_count=max(int(scheduled_offer_count), 0),
        base_planned_exposure_bnb=float(item.planned_exposure_bnb),
        allocated_budget_bnb=allocated_budget_bnb,
        allocated_budget_share=allocated_budget_share,
        price_bnb=float(resolved_price_bnb),
        recommended_delay_seconds=max(recommended_delay_seconds, 0.0),
        current_active_offers=int(item.current_active_offers),
        current_active_exposure_bnb=float(item.current_active_exposure_bnb),
        resulting_active_exposure_cap_bnb=resulting_active_exposure_cap_bnb,
        blocked_reason=blocked_reason,
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _budget_weight(
    item: MassOfferPlanItem,
    *,
    circuit_item: Any,
    caution_multiplier: float,
    global_live_block: bool,
) -> float:
    if global_live_block:
        return 0.0
    if item.status != "ready" or not item.live_eligible or item.planned_offer_count <= 0:
        return 0.0
    if circuit_item is not None and getattr(circuit_item, "severity", None) == "halt":
        return 0.0
    allocator_multiplier = _ALLOCATOR_MULTIPLIERS.get(item.band, 0.5)
    feedback_multiplier = _FEEDBACK_MULTIPLIERS.get(_feedback_band_from_notes(item.notes), 1.0)
    if allocator_multiplier <= 0 or feedback_multiplier <= 0:
        return 0.0
    weight = max(float(item.priority_score), 0.05) * allocator_multiplier * feedback_multiplier
    if circuit_item is not None and getattr(circuit_item, "severity", None) == "caution":
        weight *= max(float(caution_multiplier), 0.0)
    weight /= 1.0 + min(max(int(item.current_active_offers), 0), 10) * 0.15
    if item.exposure_headroom_bnb is not None and item.price_bnb > 0:
        headroom_slots = max(float(item.exposure_headroom_bnb) / max(float(item.price_bnb), 1e-9), 0.0)
        weight *= 1.0 + min(headroom_slots, 5.0) * 0.03
    return max(weight, 0.0)


def _feedback_band_from_notes(notes: tuple[str, ...]) -> str | None:
    for note in notes:
        if note.startswith("feedback_band="):
            value = note.split("=", 1)[1].strip().lower()
            return value or None
    return None


def _allocate_integer_slots(
    *,
    weight_map: dict[str, float],
    slot_caps: dict[str, int],
    total_slots: int,
) -> dict[str, int]:
    if total_slots <= 0:
        return {key: 0 for key in slot_caps}
    positive = {key: value for key, value in weight_map.items() if value > 0 and slot_caps.get(key, 0) > 0}
    if not positive:
        return {key: 0 for key in slot_caps}
    total_weight = sum(positive.values())
    if total_weight <= 0:
        return {key: 0 for key in slot_caps}
    allocated = {key: 0 for key in slot_caps}
    remainders: list[tuple[float, str]] = []
    assigned = 0
    for key, weight in positive.items():
        ideal = total_slots * weight / total_weight
        base = min(int(math.floor(ideal)), int(slot_caps.get(key, 0)))
        allocated[key] = base
        assigned += base
        remainders.append((ideal - math.floor(ideal), key))
    slots_left = max(total_slots - assigned, 0)
    remainders.sort(key=lambda item: (-item[0], item[1]))
    while slots_left > 0:
        progress = False
        for _, key in remainders:
            cap = int(slot_caps.get(key, 0))
            if allocated[key] >= cap:
                continue
            allocated[key] += 1
            slots_left -= 1
            progress = True
            if slots_left <= 0:
                break
        if not progress:
            break
    return allocated


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None
