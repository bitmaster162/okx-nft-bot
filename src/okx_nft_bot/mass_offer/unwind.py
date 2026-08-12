from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.analytics.portfolio_risk import PortfolioRiskAnalyzer, PortfolioRiskReport
from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.circuit_breaker import (
    MassOfferCircuitBreaker,
    MassOfferCircuitCollectionStatus,
    MassOfferCircuitReport,
)
from okx_nft_bot.mass_offer.engine import MassOfferEngine
from okx_nft_bot.mass_offer.plan import MassOfferPlanItem, MassOfferPlanReport, MassOfferPlanner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import ActiveOffer, PositionState

_ACTION_ORDER = {
    "cancel_now": 0,
    "reduce": 1,
    "review": 2,
    "keep": 3,
}


@dataclass(slots=True)
class MassOfferUnwindCandidate:
    order_hash: str
    collection_key: str
    display_name: str
    chain: str
    price_bnb: float
    age_hours: float
    current_floor: float | None
    plan_status: str
    allocation_band: str
    live_eligible: bool
    action: str
    priority_score: float
    selected: bool
    reason_code: str
    reason_detail: str | None = None
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    max_active_offers: int | None = None
    max_active_exposure_bnb: float | None = None
    exposure_overhang_bnb: float = 0.0
    offer_overhang_count: int = 0
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, int, float, float, float, str]:
        return (
            _ACTION_ORDER.get(self.action, 99),
            0 if self.selected else 1,
            -self.priority_score,
            -self.price_bnb,
            -self.age_hours,
            self.order_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_hash": self.order_hash,
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "age_hours": round(self.age_hours, 3),
            "current_floor": round(self.current_floor, 6) if self.current_floor is not None else None,
            "plan_status": self.plan_status,
            "allocation_band": self.allocation_band,
            "live_eligible": self.live_eligible,
            "action": self.action,
            "priority_score": round(self.priority_score, 3),
            "selected": self.selected,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "exposure_overhang_bnb": round(self.exposure_overhang_bnb, 6),
            "offer_overhang_count": self.offer_overhang_count,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class MassOfferUnwindReport:
    generated_at: str
    wallet: str | None
    chain: str
    window_days: int
    target_release_bnb: float
    auto_target_release_bnb: float
    max_cancels: int
    report_path: str
    plan: MassOfferPlanReport
    circuit: MassOfferCircuitReport
    risk: PortfolioRiskReport
    summary: dict[str, Any]
    candidates: list[MassOfferUnwindCandidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "window_days": self.window_days,
            "target_release_bnb": round(self.target_release_bnb, 6),
            "auto_target_release_bnb": round(self.auto_target_release_bnb, 6),
            "max_cancels": self.max_cancels,
            "report_path": self.report_path,
            "plan": self.plan.to_dict(),
            "circuit": self.circuit.to_dict(),
            "risk": self.risk.to_dict(),
            "summary": self.summary,
            "candidates": [item.to_dict() for item in self.candidates],
        }


@dataclass(slots=True)
class MassOfferUnwindExecutionResult:
    generated_at: str
    wallet: str | None
    chain: str
    requested_dry_run: bool
    effective_dry_run: bool
    selected_count: int
    attempted_count: int
    simulated_count: int
    cancelled_count: int
    failed_count: int
    selected_release_bnb: float
    failed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "requested_dry_run": self.requested_dry_run,
            "effective_dry_run": self.effective_dry_run,
            "selected_count": self.selected_count,
            "attempted_count": self.attempted_count,
            "simulated_count": self.simulated_count,
            "cancelled_count": self.cancelled_count,
            "failed_count": self.failed_count,
            "selected_release_bnb": round(self.selected_release_bnb, 6),
            "failed": list(self.failed),
        }


class MassOfferUnwindController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
        planner: MassOfferPlanner | None = None,
        circuit_breaker: MassOfferCircuitBreaker | None = None,
        risk_analyzer: PortfolioRiskAnalyzer | None = None,
        engine: MassOfferEngine | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.planner = planner or MassOfferPlanner(settings=settings, store=self.store, state=self.state)
        self.circuit = circuit_breaker or MassOfferCircuitBreaker(settings=settings, state=self.state)
        self.risk = risk_analyzer or PortfolioRiskAnalyzer(settings=settings, store=self.store, state=self.state)
        self.engine = engine or MassOfferEngine(settings=settings)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        target_release_bnb: float | None = None,
        max_cancels: int | None = None,
    ) -> MassOfferUnwindReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_unwind_window_days)
        resolved_reference_limit = int(reference_limit if reference_limit is not None else self.settings.wallet_pnl_reference_event_limit)
        resolved_event_limit = int(event_limit if event_limit is not None else self.settings.mass_offer_economics_event_limit)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        resolved_max_cancels = max(int(max_cancels if max_cancels is not None else self.settings.mass_offer_unwind_max_cancels), 1)
        requested_target_release_bnb = float(target_release_bnb if target_release_bnb is not None else self.settings.mass_offer_unwind_target_release_bnb)

        plan_report = self.planner.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=resolved_reference_limit,
            event_limit=resolved_event_limit,
            price_bnb=resolved_price_bnb,
        )
        circuit_report = self.circuit.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_hours=self.settings.mass_offer_circuit_window_hours,
        )
        risk_report = self.risk.build_report(
            wallet=wallet,
            reference_limit=resolved_reference_limit,
            chain=resolved_chain,
        )
        active_offers = self.state.get_active_offers(chain=resolved_chain)
        total_active_exposure_bnb = sum(float(item.price_bnb) for item in active_offers)
        auto_target_release_bnb = 0.0
        if requested_target_release_bnb <= 0 and active_offers:
            if risk_report.summary.block_live_submits or bool(circuit_report.summary.get("should_block_live")):
                auto_target_release_bnb = max(
                    total_active_exposure_bnb * float(self.settings.mass_offer_unwind_block_release_fraction),
                    float(self.settings.mass_offer_price_bnb),
                )
        resolved_target_release_bnb = requested_target_release_bnb if requested_target_release_bnb > 0 else auto_target_release_bnb

        plan_map = {item.collection_key: item for item in plan_report.collections}
        circuit_map = {item.collection_key: item for item in circuit_report.collections}
        offers_by_collection: dict[str, list[ActiveOffer]] = {}
        for offer in active_offers:
            offers_by_collection.setdefault(offer.collection, []).append(offer)

        forced_hashes: set[str] = set()
        candidates: list[MassOfferUnwindCandidate] = []
        for collection_key, collection_offers in offers_by_collection.items():
            plan_item = plan_map.get(collection_key)
            circuit_item = circuit_map.get(collection_key)
            sorted_for_reduce = sorted(
                collection_offers,
                key=lambda item: (-float(item.price_bnb), -item.age_hours, item.order_hash),
            )
            active_count = len(collection_offers)
            exposure_bnb = sum(float(item.price_bnb) for item in collection_offers)
            max_active_offers = int(plan_item.max_active_offers) if plan_item and plan_item.max_active_offers is not None else None
            max_active_exposure_bnb = float(plan_item.max_active_exposure_bnb) if plan_item and plan_item.max_active_exposure_bnb is not None else None
            offer_overhang_count = max(active_count - max_active_offers, 0) if max_active_offers is not None else 0
            exposure_overhang_bnb = (
                max(exposure_bnb - max_active_exposure_bnb, 0.0)
                if max_active_exposure_bnb is not None
                else 0.0
            )
            collection_force_all = _collection_force_all(plan_item)
            if collection_force_all:
                forced_hashes.update(item.order_hash for item in collection_offers)
            elif offer_overhang_count > 0 or exposure_overhang_bnb > 1e-12:
                forced_hashes.update(
                    _pick_reduce_hashes(
                        offers=sorted_for_reduce,
                        needed_count=offer_overhang_count,
                        needed_release_bnb=exposure_overhang_bnb,
                    )
                )
            for offer in collection_offers:
                candidate = _build_candidate(
                    offer=offer,
                    plan_item=plan_item,
                    circuit_item=circuit_item,
                    risk_report=risk_report,
                    active_count=active_count,
                    exposure_bnb=exposure_bnb,
                    max_active_offers=max_active_offers,
                    max_active_exposure_bnb=max_active_exposure_bnb,
                    offer_overhang_count=offer_overhang_count,
                    exposure_overhang_bnb=exposure_overhang_bnb,
                    forced_hashes=forced_hashes,
                    global_circuit_blocks=bool(circuit_report.summary.get("should_block_live")),
                    stale_offer_hours=max(int(self.settings.mass_offer_duration_hours), 1),
                )
                candidates.append(candidate)

        candidates.sort(key=lambda item: item.sort_key())
        selected_order_hashes = _select_candidates(
            candidates=candidates,
            max_cancels=resolved_max_cancels,
            target_release_bnb=resolved_target_release_bnb,
            forced_hashes=forced_hashes,
        )
        selected_release_bnb = 0.0
        selected_cancel_now_count = 0
        selected_reduce_count = 0
        with_selection: list[MassOfferUnwindCandidate] = []
        for item in candidates:
            selected = item.order_hash in selected_order_hashes
            if selected:
                selected_release_bnb += float(item.price_bnb)
                if item.action == "cancel_now":
                    selected_cancel_now_count += 1
                elif item.action == "reduce":
                    selected_reduce_count += 1
            with_selection.append(
                MassOfferUnwindCandidate(
                    order_hash=item.order_hash,
                    collection_key=item.collection_key,
                    display_name=item.display_name,
                    chain=item.chain,
                    price_bnb=item.price_bnb,
                    age_hours=item.age_hours,
                    current_floor=item.current_floor,
                    plan_status=item.plan_status,
                    allocation_band=item.allocation_band,
                    live_eligible=item.live_eligible,
                    action=item.action,
                    priority_score=item.priority_score,
                    selected=selected,
                    reason_code=item.reason_code,
                    reason_detail=item.reason_detail,
                    current_active_offers=item.current_active_offers,
                    current_active_exposure_bnb=item.current_active_exposure_bnb,
                    max_active_offers=item.max_active_offers,
                    max_active_exposure_bnb=item.max_active_exposure_bnb,
                    exposure_overhang_bnb=item.exposure_overhang_bnb,
                    offer_overhang_count=item.offer_overhang_count,
                    notes=item.notes,
                )
            )
        with_selection.sort(key=lambda item: item.sort_key())

        top_item = next((item for item in with_selection if item.selected), with_selection[0] if with_selection else None)
        summary = {
            "active_offer_count": len(active_offers),
            "active_exposure_bnb": round(total_active_exposure_bnb, 6),
            "collection_count": len(offers_by_collection),
            "target_release_bnb": round(resolved_target_release_bnb, 6),
            "auto_target_release_bnb": round(auto_target_release_bnb, 6),
            "selected_count": len(selected_order_hashes),
            "selected_release_bnb": round(selected_release_bnb, 6),
            "selected_cancel_now_count": selected_cancel_now_count,
            "selected_reduce_count": selected_reduce_count,
            "cancel_now_count": sum(1 for item in with_selection if item.action == "cancel_now"),
            "reduce_count": sum(1 for item in with_selection if item.action == "reduce"),
            "review_count": sum(1 for item in with_selection if item.action == "review"),
            "keep_count": sum(1 for item in with_selection if item.action == "keep"),
            "forced_count": len(forced_hashes),
            "forced_unselected_count": max(len(forced_hashes) - len([item for item in with_selection if item.selected and item.order_hash in forced_hashes]), 0),
            "risk_blocks_live": bool(risk_report.summary.block_live_submits),
            "risk_severity": risk_report.summary.severity,
            "circuit_should_block_live": bool(circuit_report.summary.get("should_block_live")),
            "circuit_severity": circuit_report.summary.get("severity"),
            "top_collection": top_item.collection_key if top_item is not None else None,
            "top_reason": top_item.reason_code if top_item is not None else None,
            "top_action": top_item.action if top_item is not None else None,
        }
        return MassOfferUnwindReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            window_days=resolved_window_days,
            target_release_bnb=resolved_target_release_bnb,
            auto_target_release_bnb=auto_target_release_bnb,
            max_cancels=resolved_max_cancels,
            report_path=str(self.settings.mass_offer_unwind_report_path),
            plan=plan_report,
            circuit=circuit_report,
            risk=risk_report,
            summary=summary,
            candidates=with_selection,
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
        target_release_bnb: float | None = None,
        max_cancels: int | None = None,
        report_path: Path | None = None,
    ) -> str:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
            target_release_bnb=target_release_bnb,
            max_cancels=max_cancels,
        )
        resolved_report_path = report_path or self.settings.mass_offer_unwind_report_path
        _write_json(resolved_report_path, report.to_dict())
        self._persist_runtime_summary(report, report_path=resolved_report_path)
        return str(resolved_report_path)

    def execute_report(
        self,
        report: MassOfferUnwindReport,
        *,
        dry_run: bool = True,
    ) -> MassOfferUnwindExecutionResult:
        selected_hashes = [item.order_hash for item in report.candidates if item.selected]
        selected_release_bnb = sum(float(item.price_bnb) for item in report.candidates if item.selected)
        requested_dry_run = bool(dry_run)
        effective_dry_run = self.engine.governor.effective_dry_run(requested_dry_run)
        if not selected_hashes:
            result = MassOfferUnwindExecutionResult(
                generated_at=datetime.now(timezone.utc).isoformat(),
                wallet=report.wallet,
                chain=report.chain,
                requested_dry_run=requested_dry_run,
                effective_dry_run=effective_dry_run,
                selected_count=0,
                attempted_count=0,
                simulated_count=0,
                cancelled_count=0,
                failed_count=0,
                selected_release_bnb=0.0,
                failed=(),
            )
            self._persist_runtime_summary(report, execution=result)
            return result
        if effective_dry_run:
            result = MassOfferUnwindExecutionResult(
                generated_at=datetime.now(timezone.utc).isoformat(),
                wallet=report.wallet,
                chain=report.chain,
                requested_dry_run=requested_dry_run,
                effective_dry_run=True,
                selected_count=len(selected_hashes),
                attempted_count=len(selected_hashes),
                simulated_count=len(selected_hashes),
                cancelled_count=0,
                failed_count=0,
                selected_release_bnb=selected_release_bnb,
                failed=(),
            )
            self._persist_runtime_summary(report, execution=result)
            return result
        payload = self.engine.cancel_selected(chain=report.chain, order_hashes=selected_hashes)
        cancelled_count = int(payload.get("cancelled") or 0)
        failed = tuple(str(item) for item in payload.get("failed", []) if str(item).strip())
        result = MassOfferUnwindExecutionResult(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=report.wallet,
            chain=report.chain,
            requested_dry_run=False,
            effective_dry_run=False,
            selected_count=len(selected_hashes),
            attempted_count=int(payload.get("selected_seen") or len(selected_hashes)),
            simulated_count=0,
            cancelled_count=cancelled_count,
            failed_count=len(failed),
            selected_release_bnb=selected_release_bnb,
            failed=failed,
        )
        self._persist_runtime_summary(report, execution=result)
        return result

    def _persist_runtime_summary(
        self,
        report: MassOfferUnwindReport,
        *,
        report_path: Path | None = None,
        execution: MassOfferUnwindExecutionResult | None = None,
    ) -> None:
        self.state.set_runtime_value("last_mass_offer_unwind_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_unwind_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_unwind_wallet", report.wallet)
        self.state.set_runtime_value("last_mass_offer_unwind_window_days", report.window_days)
        self.state.set_runtime_value("last_mass_offer_unwind_target_release_bnb", report.summary.get("target_release_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_unwind_auto_target_release_bnb", report.summary.get("auto_target_release_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_unwind_active_offer_count", report.summary.get("active_offer_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_active_exposure_bnb", report.summary.get("active_exposure_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_unwind_selected_count", report.summary.get("selected_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_selected_release_bnb", report.summary.get("selected_release_bnb", 0.0))
        self.state.set_runtime_value("last_mass_offer_unwind_cancel_now_count", report.summary.get("cancel_now_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_reduce_count", report.summary.get("reduce_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_review_count", report.summary.get("review_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_keep_count", report.summary.get("keep_count", 0))
        self.state.set_runtime_value("last_mass_offer_unwind_risk_blocks_live", "1" if report.summary.get("risk_blocks_live") else "0")
        self.state.set_runtime_value("last_mass_offer_unwind_circuit_blocks_live", "1" if report.summary.get("circuit_should_block_live") else "0")
        self.state.set_runtime_value("last_mass_offer_unwind_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_unwind_top_reason", report.summary.get("top_reason"))
        self.state.set_runtime_value("last_mass_offer_unwind_top_action", report.summary.get("top_action"))
        self.state.set_runtime_value("last_mass_offer_unwind_report_path", str(report_path or report.report_path))
        if execution is not None:
            self.state.set_runtime_value("last_mass_offer_unwind_execute_at", execution.generated_at)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_requested_dry_run", "1" if execution.requested_dry_run else "0")
            self.state.set_runtime_value("last_mass_offer_unwind_execute_effective_dry_run", "1" if execution.effective_dry_run else "0")
            self.state.set_runtime_value("last_mass_offer_unwind_execute_selected_count", execution.selected_count)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_attempted_count", execution.attempted_count)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_simulated_count", execution.simulated_count)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_cancelled_count", execution.cancelled_count)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_failed_count", execution.failed_count)
            self.state.set_runtime_value("last_mass_offer_unwind_execute_selected_release_bnb", execution.selected_release_bnb)


def get_mass_offer_unwind_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_unwind_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_unwind_chain"),
        "wallet": runtime.get("last_mass_offer_unwind_wallet"),
        "window_days": _coerce_int(runtime.get("last_mass_offer_unwind_window_days")),
        "target_release_bnb": _coerce_float(runtime.get("last_mass_offer_unwind_target_release_bnb")),
        "auto_target_release_bnb": _coerce_float(runtime.get("last_mass_offer_unwind_auto_target_release_bnb")),
        "active_offer_count": _coerce_int(runtime.get("last_mass_offer_unwind_active_offer_count")),
        "active_exposure_bnb": _coerce_float(runtime.get("last_mass_offer_unwind_active_exposure_bnb")),
        "selected_count": _coerce_int(runtime.get("last_mass_offer_unwind_selected_count")),
        "selected_release_bnb": _coerce_float(runtime.get("last_mass_offer_unwind_selected_release_bnb")),
        "cancel_now_count": _coerce_int(runtime.get("last_mass_offer_unwind_cancel_now_count")),
        "reduce_count": _coerce_int(runtime.get("last_mass_offer_unwind_reduce_count")),
        "review_count": _coerce_int(runtime.get("last_mass_offer_unwind_review_count")),
        "keep_count": _coerce_int(runtime.get("last_mass_offer_unwind_keep_count")),
        "risk_blocks_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_unwind_risk_blocks_live"))),
        "circuit_blocks_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_unwind_circuit_blocks_live"))),
        "top_collection": runtime.get("last_mass_offer_unwind_top_collection"),
        "top_reason": runtime.get("last_mass_offer_unwind_top_reason"),
        "top_action": runtime.get("last_mass_offer_unwind_top_action"),
        "report_path": runtime.get("last_mass_offer_unwind_report_path"),
        "execute_at": runtime.get("last_mass_offer_unwind_execute_at"),
        "execute_requested_dry_run": bool(_coerce_optional_bool(runtime.get("last_mass_offer_unwind_execute_requested_dry_run"))),
        "execute_effective_dry_run": bool(_coerce_optional_bool(runtime.get("last_mass_offer_unwind_execute_effective_dry_run"))),
        "execute_selected_count": _coerce_int(runtime.get("last_mass_offer_unwind_execute_selected_count")),
        "execute_attempted_count": _coerce_int(runtime.get("last_mass_offer_unwind_execute_attempted_count")),
        "execute_simulated_count": _coerce_int(runtime.get("last_mass_offer_unwind_execute_simulated_count")),
        "execute_cancelled_count": _coerce_int(runtime.get("last_mass_offer_unwind_execute_cancelled_count")),
        "execute_failed_count": _coerce_int(runtime.get("last_mass_offer_unwind_execute_failed_count")),
        "execute_selected_release_bnb": _coerce_float(runtime.get("last_mass_offer_unwind_execute_selected_release_bnb")),
    }


def format_mass_offer_unwind_text(report: MassOfferUnwindReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_unwind",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"window_days={report.window_days}",
        (
            f"active={report.summary.get('active_offer_count', 0)} exposure={float(report.summary.get('active_exposure_bnb', 0.0)):.6f} "
            f"selected={report.summary.get('selected_count', 0)} release={float(report.summary.get('selected_release_bnb', 0.0)):.6f}"
        ),
        (
            f"target={float(report.summary.get('target_release_bnb', 0.0)):.6f} "
            f"risk_block={bool(report.summary.get('risk_blocks_live'))} circuit_block={bool(report.summary.get('circuit_should_block_live'))}"
        ),
        (
            f"cancel_now={report.summary.get('cancel_now_count', 0)} reduce={report.summary.get('reduce_count', 0)} "
            f"review={report.summary.get('review_count', 0)} keep={report.summary.get('keep_count', 0)}"
        ),
    ]
    for item in report.candidates[: max(int(limit), 1)]:
        marker = "*" if item.selected else "-"
        lines.append(
            (
                f"{marker} {item.display_name} [{item.action}] | price={item.price_bnb:.6f} | age={item.age_hours:.1f}h | "
                f"plan={item.plan_status}/{item.allocation_band} | score={item.priority_score:.1f} | reason={item.reason_code}"
            )
        )
        if item.reason_detail:
            lines.append(f"  detail={item.reason_detail}")
    return "\n".join(lines)


def format_mass_offer_unwind_execution_text(result: MassOfferUnwindExecutionResult) -> str:
    lines = [
        "mass_offer_unwind_execute",
        f"chain={result.chain}",
        f"dry_run={result.effective_dry_run}",
        (
            f"selected={result.selected_count} attempted={result.attempted_count} simulated={result.simulated_count} "
            f"cancelled={result.cancelled_count} failed={result.failed_count} release={result.selected_release_bnb:.6f}"
        ),
    ]
    for item in result.failed[:5]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _collection_force_all(plan_item: MassOfferPlanItem | None) -> bool:
    if plan_item is None:
        return False
    if plan_item.status in {"blocked", "risk_blocked"}:
        return True
    if plan_item.dry_run_only:
        return True
    return False


def _pick_reduce_hashes(
    *,
    offers: list[ActiveOffer],
    needed_count: int,
    needed_release_bnb: float,
) -> set[str]:
    selected: set[str] = set()
    remaining_count = max(int(needed_count), 0)
    remaining_release = max(float(needed_release_bnb), 0.0)
    for offer in offers:
        if remaining_count <= 0 and remaining_release <= 1e-12:
            break
        selected.add(offer.order_hash)
        remaining_count = max(remaining_count - 1, 0)
        remaining_release = max(remaining_release - float(offer.price_bnb), 0.0)
    return selected


def _build_candidate(
    *,
    offer: ActiveOffer,
    plan_item: MassOfferPlanItem | None,
    circuit_item: MassOfferCircuitCollectionStatus | None,
    risk_report: PortfolioRiskReport,
    active_count: int,
    exposure_bnb: float,
    max_active_offers: int | None,
    max_active_exposure_bnb: float | None,
    offer_overhang_count: int,
    exposure_overhang_bnb: float,
    forced_hashes: set[str],
    global_circuit_blocks: bool,
    stale_offer_hours: int,
) -> MassOfferUnwindCandidate:
    display_name = plan_item.display_name if plan_item is not None else offer.collection
    plan_status = plan_item.status if plan_item is not None else "unplanned"
    allocation_band = plan_item.band if plan_item is not None else "unknown"
    live_eligible = bool(plan_item.live_eligible) if plan_item is not None else False
    score = 0.0
    action = "keep"
    reason_code = "healthy"
    reason_detail: str | None = None
    notes: list[str] = []

    if offer.order_hash in forced_hashes:
        if plan_item is not None and plan_item.status in {"blocked", "risk_blocked"}:
            score += 100.0
            action = "cancel_now"
            reason_code = "policy_blocked"
            reason_detail = plan_item.status
        elif plan_item is not None and plan_item.dry_run_only:
            score += 90.0
            action = "cancel_now"
            reason_code = "policy_dry_run_only"
            reason_detail = "collection no longer live-eligible"
        else:
            score += 72.0
            action = "reduce"
            reason_code = "cap_overhang"
            reason_detail = f"offers>{max_active_offers}" if offer_overhang_count > 0 else f"exposure>{max_active_exposure_bnb}"
    elif plan_item is None:
        score += 14.0
        action = "review"
        reason_code = "unplanned_active_offer"
        reason_detail = "collection missing from current mass-offer plan"
    elif plan_item.status == "capped_out":
        score += 32.0
        action = "reduce"
        reason_code = "collection_capped"
        reason_detail = "collection already at policy cap"
    elif plan_item.status == "rate_limited":
        score += 6.0
        action = "review"
        reason_code = "rate_limited"
        reason_detail = "global/live rate guard currently tight"

    if circuit_item is not None:
        if circuit_item.severity == "halt":
            score += 36.0
            if _ACTION_ORDER[action] > _ACTION_ORDER["reduce"]:
                action = "reduce"
            if reason_code == "healthy":
                reason_code = "circuit_halt"
                reason_detail = circuit_item.issue_code
        elif circuit_item.severity == "caution":
            score += 14.0
            if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
                action = "review"
            if reason_code == "healthy":
                reason_code = "circuit_caution"
                reason_detail = circuit_item.issue_code
        notes.append(f"circuit={circuit_item.severity}")

    if risk_report.summary.block_live_submits:
        score += 18.0
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"
        if reason_code == "healthy":
            reason_code = "global_risk_block"
            reason_detail = risk_report.summary.top_breach_code
    elif risk_report.summary.severity in {"HIGH_RISK", "CAUTION"}:
        score += 8.0
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"

    if global_circuit_blocks:
        score += 8.0
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"

    if offer.age_hours >= float(max(stale_offer_hours * 2, 24)):
        score += 18.0
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"
        notes.append("stale_offer_2x")
        if reason_code == "healthy":
            reason_code = "stale_offer"
            reason_detail = f"age>={max(stale_offer_hours * 2, 24)}h"
    elif offer.age_hours >= float(max(stale_offer_hours, 12)):
        score += 9.0
        notes.append("stale_offer")
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"
        if reason_code == "healthy":
            reason_code = "stale_offer"
            reason_detail = f"age>={max(stale_offer_hours, 12)}h"

    if offer.current_floor is not None and offer.current_floor > 0 and offer.price_bnb > offer.current_floor * 1.5:
        score += 7.0
        notes.append("far_above_floor")
        if _ACTION_ORDER[action] > _ACTION_ORDER["review"]:
            action = "review"
        if reason_code == "healthy":
            reason_code = "far_above_floor"
            reason_detail = f"offer>{offer.current_floor * 1.5:.6f} floor-adjusted"

    return MassOfferUnwindCandidate(
        order_hash=offer.order_hash,
        collection_key=offer.collection,
        display_name=display_name,
        chain=offer.chain,
        price_bnb=float(offer.price_bnb),
        age_hours=float(offer.age_hours),
        current_floor=float(offer.current_floor) if offer.current_floor is not None else None,
        plan_status=plan_status,
        allocation_band=allocation_band,
        live_eligible=live_eligible,
        action=action,
        priority_score=score,
        selected=False,
        reason_code=reason_code,
        reason_detail=reason_detail,
        current_active_offers=active_count,
        current_active_exposure_bnb=round(float(exposure_bnb), 6),
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=round(float(max_active_exposure_bnb), 6) if max_active_exposure_bnb is not None else None,
        exposure_overhang_bnb=round(float(exposure_overhang_bnb), 6),
        offer_overhang_count=int(offer_overhang_count),
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _select_candidates(
    *,
    candidates: list[MassOfferUnwindCandidate],
    max_cancels: int,
    target_release_bnb: float,
    forced_hashes: set[str],
) -> set[str]:
    selected: set[str] = set()
    selected_release_bnb = 0.0
    remaining = max(int(max_cancels), 1)
    for item in candidates:
        if remaining <= 0:
            break
        if item.order_hash not in forced_hashes:
            continue
        selected.add(item.order_hash)
        selected_release_bnb += float(item.price_bnb)
        remaining -= 1
    if target_release_bnb <= 1e-12 or remaining <= 0:
        return selected
    for item in candidates:
        if remaining <= 0 or selected_release_bnb >= target_release_bnb - 1e-12:
            break
        if item.order_hash in selected:
            continue
        if item.action not in {"cancel_now", "reduce"}:
            continue
        selected.add(item.order_hash)
        selected_release_bnb += float(item.price_bnb)
        remaining -= 1
    return selected


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
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
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
