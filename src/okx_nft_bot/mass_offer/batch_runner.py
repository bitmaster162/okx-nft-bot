from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.budget_scheduler import MassOfferBudgetScheduler
from okx_nft_bot.mass_offer.rebalancer import MassOfferBudgetRebalancer
from okx_nft_bot.mass_offer.circuit_breaker import (
    MassOfferCircuitBreaker,
    MassOfferCircuitReport,
)
from okx_nft_bot.mass_offer.engine import MassOfferEngine, MassOfferRunResult
from okx_nft_bot.mass_offer.feedback import MassOfferFeedbackController
from okx_nft_bot.mass_offer.plan import MassOfferPlanItem, MassOfferPlanner
from okx_nft_bot.mass_offer.quarantine import MassOfferQuarantineController
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MassOfferBatchCollectionResult:
    collection_key: str
    display_name: str
    chain: str
    plan_status: str
    batch_status: str
    band: str
    live_eligible: bool
    requested_dry_run: bool | None
    effective_dry_run: bool
    planned_offer_count: int
    planned_exposure_bnb: float
    recommended_delay_seconds: float
    campaign_id: int | None = None
    submitted_count: int = 0
    dry_run_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    blocked_reason: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "plan_status": self.plan_status,
            "batch_status": self.batch_status,
            "band": self.band,
            "live_eligible": self.live_eligible,
            "requested_dry_run": self.requested_dry_run,
            "effective_dry_run": self.effective_dry_run,
            "planned_offer_count": self.planned_offer_count,
            "planned_exposure_bnb": round(self.planned_exposure_bnb, 6),
            "recommended_delay_seconds": round(self.recommended_delay_seconds, 6),
            "campaign_id": self.campaign_id,
            "submitted_count": self.submitted_count,
            "dry_run_count": self.dry_run_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class MassOfferBatchRunReport:
    generated_at: str
    wallet: str | None
    chain: str
    price_bnb: float
    window_days: int
    collection_limit: int
    include_dry_run_collections: bool
    requested_dry_run: bool | None
    effective_batch_dry_run: bool
    report_path: str
    summary: dict[str, Any]
    collections: list[MassOfferBatchCollectionResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "window_days": self.window_days,
            "collection_limit": self.collection_limit,
            "include_dry_run_collections": self.include_dry_run_collections,
            "requested_dry_run": self.requested_dry_run,
            "effective_batch_dry_run": self.effective_batch_dry_run,
            "report_path": self.report_path,
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }


class MassOfferBatchRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        planner: MassOfferPlanner | None = None,
        engine: MassOfferEngine | None = None,
        state: PositionState | None = None,
        feedback_controller: MassOfferFeedbackController | None = None,
        circuit_breaker: MassOfferCircuitBreaker | None = None,
        budget_scheduler: MassOfferBudgetScheduler | None = None,
        rebalancer: MassOfferBudgetRebalancer | None = None,
        quarantine_controller: MassOfferQuarantineController | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.planner = planner or MassOfferPlanner(settings=settings, store=self.store, state=self.state)
        self.engine = engine or MassOfferEngine(settings=settings)
        planner_feedback = getattr(self.planner, "feedback", None)
        self.feedback = feedback_controller or planner_feedback or MassOfferFeedbackController(
            settings=settings,
            store=self.store,
            state=self.state,
        )
        self.circuit = circuit_breaker or MassOfferCircuitBreaker(
            settings=settings,
            state=self.state,
        )
        self.budget = budget_scheduler or MassOfferBudgetScheduler(
            settings=settings,
            store=self.store,
            state=self.state,
        )
        self.rebalancer = rebalancer or MassOfferBudgetRebalancer(
            settings=settings,
            store=self.store,
            state=self.state,
        )
        self.quarantine = quarantine_controller or MassOfferQuarantineController(
            settings=settings,
            store=self.store,
            state=self.state,
            rebalancer=self.rebalancer,
        )

    def run_batch(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        collection_limit: int | None = None,
        include_dry_run_collections: bool | None = None,
        dry_run: bool | None = None,
        write_report: bool = False,
        report_path: Path | None = None,
    ) -> MassOfferBatchRunReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_allocator_window_days)
        resolved_reference_limit = int(reference_limit if reference_limit is not None else self.settings.wallet_pnl_reference_event_limit)
        resolved_event_limit = int(event_limit if event_limit is not None else self.settings.mass_offer_economics_event_limit)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        resolved_collection_limit = max(int(collection_limit if collection_limit is not None else self.settings.mass_offer_batch_collection_limit), 1)
        requested_batch_dry_run = dry_run if dry_run is not None else self.settings.mass_offer_dry_run
        effective_batch_dry_run = self.engine.governor.effective_dry_run(requested_batch_dry_run)
        resolved_sync_before_run = bool(self.settings.mass_offer_batch_sync_policies_before_run)
        resolved_refresh_after_run = bool(self.settings.mass_offer_batch_refresh_policies_after_run)
        resolved_sync_budget_before_run = bool(self.settings.mass_offer_batch_sync_budget_before_run)
        resolved_refresh_budget_after_run = bool(self.settings.mass_offer_batch_refresh_budget_after_run)
        resolved_sync_rebalance_before_run = bool(
            resolved_sync_before_run and self.settings.mass_offer_batch_sync_rebalance_before_run
        )
        resolved_refresh_rebalance_after_run = bool(
            resolved_refresh_after_run and self.settings.mass_offer_batch_refresh_rebalance_after_run
        )
        resolved_sync_quarantine_before_run = bool(
            resolved_sync_before_run and self.settings.mass_offer_batch_sync_quarantine_before_run
        )
        resolved_refresh_quarantine_after_run = bool(
            resolved_refresh_after_run and self.settings.mass_offer_batch_refresh_quarantine_after_run
        )
        resolved_check_circuit_before = bool(self.settings.mass_offer_circuit_enabled and self.settings.mass_offer_batch_check_circuit_before_run)
        resolved_check_circuit_during = bool(self.settings.mass_offer_circuit_enabled and self.settings.mass_offer_batch_check_circuit_during_run)
        resolved_include_dry_run = (
            bool(include_dry_run_collections)
            if include_dry_run_collections is not None
            else bool(self.settings.mass_offer_batch_include_dry_run_collections or effective_batch_dry_run)
        )

        pre_policy_sync = self._maybe_sync_policies(
            enabled=resolved_sync_before_run,
            include_budget=resolved_sync_budget_before_run,
            include_rebalance=resolved_sync_rebalance_before_run,
            include_quarantine=resolved_sync_quarantine_before_run,
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=resolved_reference_limit,
            event_limit=resolved_event_limit,
            price_bnb=resolved_price_bnb,
            reason_if_skipped="disabled",
        )
        pre_circuit = self._maybe_check_circuit(
            enabled=resolved_check_circuit_before and not effective_batch_dry_run,
            wallet=wallet,
            chain=resolved_chain,
            reason_if_skipped="batch_dry_run" if effective_batch_dry_run else "disabled",
        )

        initial_plan = self.planner.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=resolved_reference_limit,
            event_limit=resolved_event_limit,
            price_bnb=resolved_price_bnb,
        )
        processed: list[MassOfferBatchCollectionResult] = []
        seen_collections: set[str] = set()
        last_plan = initial_plan
        stopped_reason = "collection_limit_reached"
        mid_circuit: dict[str, Any] | None = None

        if not effective_batch_dry_run and pre_circuit.get("should_block_live"):
            stopped_reason = f"circuit:{pre_circuit.get('issue_code') or 'halt'}"
        else:
            for _ in range(resolved_collection_limit):
                current_plan = self.planner.build_report(
                    wallet=wallet,
                    chain=resolved_chain,
                    window_days=resolved_window_days,
                    reference_limit=resolved_reference_limit,
                    event_limit=resolved_event_limit,
                    price_bnb=resolved_price_bnb,
                )
                last_plan = current_plan
                candidate = _pick_next_candidate(
                    current_plan.collections,
                    seen=seen_collections,
                    include_dry_run_collections=resolved_include_dry_run,
                )
                if candidate is None:
                    stopped_reason = "no_eligible_collections"
                    break
                seen_collections.add(candidate.collection_key)
                result = self._run_single_collection(
                    plan_item=candidate,
                    chain=resolved_chain,
                    price_bnb=resolved_price_bnb,
                    dry_run=dry_run,
                )
                processed.append(result)
                if result.batch_status == "blocked" and result.blocked_reason and _is_global_live_block(result.blocked_reason):
                    stopped_reason = f"blocked:{result.blocked_reason}"
                    break
                if resolved_check_circuit_during and not result.effective_dry_run:
                    mid_circuit = self._maybe_check_circuit(
                        enabled=True,
                        wallet=wallet,
                        chain=resolved_chain,
                        reason_if_skipped="disabled",
                    )
                    if mid_circuit.get("should_block_live"):
                        stopped_reason = f"circuit:{mid_circuit.get('issue_code') or 'halt'}"
                        break
            else:
                stopped_reason = "collection_limit_reached"

        post_policy_sync = self._maybe_sync_policies(
            enabled=resolved_refresh_after_run and any(item.campaign_id is not None for item in processed),
            include_budget=resolved_refresh_budget_after_run and bool(processed),
            include_rebalance=resolved_refresh_rebalance_after_run and bool(processed),
            include_quarantine=resolved_refresh_quarantine_after_run and bool(processed),
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=resolved_reference_limit,
            event_limit=resolved_event_limit,
            price_bnb=resolved_price_bnb,
            reason_if_skipped=("no_campaigns" if processed else "nothing_selected") if (resolved_refresh_after_run or resolved_refresh_budget_after_run or resolved_refresh_quarantine_after_run) else "disabled",
        )
        post_circuit = self._maybe_check_circuit(
            enabled=bool(self.settings.mass_offer_circuit_enabled),
            wallet=wallet,
            chain=resolved_chain,
            reason_if_skipped="disabled",
        )

        last_plan = self.planner.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=resolved_reference_limit,
            event_limit=resolved_event_limit,
            price_bnb=resolved_price_bnb,
        )
        remaining = [item for item in last_plan.collections if item.collection_key not in seen_collections]
        summary = {
            "initial_ready_count": initial_plan.summary.get("ready_count", 0),
            "initial_dry_run_only_count": initial_plan.summary.get("dry_run_only_count", 0),
            "selected_count": len(processed),
            "executed_count": sum(1 for item in processed if item.batch_status in {"executed_live", "executed_dry_run", "partial"}),
            "executed_live_count": sum(1 for item in processed if item.batch_status in {"executed_live", "partial"} and not item.effective_dry_run),
            "executed_dry_run_count": sum(1 for item in processed if item.batch_status == "executed_dry_run" or item.effective_dry_run),
            "blocked_count": sum(1 for item in processed if item.batch_status == "blocked"),
            "failed_count": sum(1 for item in processed if item.batch_status == "failed"),
            "submitted_count": sum(item.submitted_count for item in processed),
            "dry_run_offer_count": sum(item.dry_run_count for item in processed),
            "failed_offer_count": sum(item.failed_count for item in processed),
            "skipped_offer_count": sum(item.skipped_count for item in processed),
            "remaining_ready_count": sum(1 for item in remaining if item.status == "ready"),
            "remaining_dry_run_only_count": sum(1 for item in remaining if item.status == "dry_run_only"),
            "remaining_rate_limited_count": sum(1 for item in remaining if item.status == "rate_limited"),
            "remaining_risk_blocked_count": sum(1 for item in remaining if item.status == "risk_blocked"),
            "remaining_blocked_count": sum(1 for item in remaining if item.status == "blocked"),
            "remaining_capped_out_count": sum(1 for item in remaining if item.status == "capped_out"),
            "stopped_reason": stopped_reason,
            "top_selected_collection": processed[0].collection_key if processed else None,
            "top_selected_status": processed[0].batch_status if processed else None,
            "plan_ready_count": last_plan.summary.get("ready_count", 0),
            "plan_dry_run_only_count": last_plan.summary.get("dry_run_only_count", 0),
            "plan_risk_blocks_live": last_plan.summary.get("risk_blocks_live", False),
            "plan_rate_limited": last_plan.summary.get("rate_limited", False),
            "pre_policy_sync": pre_policy_sync,
            "post_policy_sync": post_policy_sync,
            "pre_circuit": pre_circuit,
            "mid_circuit": mid_circuit,
            "post_circuit": post_circuit,
        }
        report = MassOfferBatchRunReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            window_days=resolved_window_days,
            collection_limit=resolved_collection_limit,
            include_dry_run_collections=resolved_include_dry_run,
            requested_dry_run=dry_run,
            effective_batch_dry_run=effective_batch_dry_run,
            report_path=str(report_path or self.settings.mass_offer_batch_report_path),
            summary=summary,
            collections=processed,
        )
        self._persist_runtime_summary(report)
        if write_report:
            resolved_report_path = report_path or self.settings.mass_offer_batch_report_path
            resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def write_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
        collection_limit: int | None = None,
        include_dry_run_collections: bool | None = None,
        dry_run: bool | None = None,
        report_path: Path | None = None,
    ) -> str:
        report = self.run_batch(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
            collection_limit=collection_limit,
            include_dry_run_collections=include_dry_run_collections,
            dry_run=dry_run,
            write_report=True,
            report_path=report_path,
        )
        return report.report_path

    def _maybe_sync_policies(
        self,
        *,
        enabled: bool,
        include_budget: bool,
        include_rebalance: bool,
        include_quarantine: bool,
        wallet: str | None,
        chain: str,
        window_days: int,
        reference_limit: int,
        event_limit: int,
        price_bnb: float,
        reason_if_skipped: str,
    ) -> dict[str, Any]:
        if not enabled and not include_budget and not include_rebalance and not include_quarantine:
            return {
                "status": "skipped",
                "refreshed": False,
                "budget_refreshed": False,
                "rebalance_refreshed": False,
                "quarantine_refreshed": False,
                "reason": reason_if_skipped,
            }
        payload: dict[str, Any] = {
            "status": "ok",
            "refreshed": False,
            "budget_refreshed": False,
            "rebalance_refreshed": False,
            "quarantine_refreshed": False,
            "reason": None,
        }
        errors: list[str] = []
        if enabled:
            try:
                result = self.feedback.sync_policy_bundle(
                    wallet=wallet,
                    chain=chain,
                    window_days=window_days,
                    reference_limit=reference_limit,
                    event_limit=event_limit,
                )
                payload.update(result.to_dict())
                payload.update(
                    {
                        "refreshed": True,
                        "allocator_policy_entries": result.allocator_summary.get("policy_entries", 0),
                        "feedback_policy_entries": result.feedback_summary.get("policy_entries", 0),
                        "top_collection": result.feedback_summary.get("top_collection"),
                        "top_band": result.feedback_summary.get("top_band"),
                    }
                )
            except Exception as exc:  # pragma: no cover - batch execution should remain usable when sync fails
                logger.warning("Mass-offer policy sync failed: %s", exc)
                errors.append(f"policy={repr(exc)}")
        if include_budget:
            try:
                budget_result = self.budget.sync_policy(
                    wallet=wallet,
                    chain=chain,
                    window_days=window_days,
                    reference_limit=reference_limit,
                    event_limit=event_limit,
                    price_bnb=price_bnb,
                )
                payload.update(
                    {
                        "budget_refreshed": True,
                        "budget_generated_at": budget_result.generated_at,
                        "budget_report_path": budget_result.report_path,
                        "budget_policy_path": budget_result.policy_path,
                        "budget_policy_entries": budget_result.summary.get("policy_entries", 0),
                        "budget_top_collection": budget_result.summary.get("top_collection"),
                        "budget_top_band": budget_result.summary.get("top_band"),
                        "budget_allocated_total_bnb": budget_result.summary.get("allocated_total_bnb", 0.0),
                        "budget_allocated_total_slots": budget_result.summary.get("allocated_total_slots", 0),
                    }
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Mass-offer budget sync failed: %s", exc)
                errors.append(f"budget={repr(exc)}")
        if include_rebalance:
            try:
                rebalance_result = self.rebalancer.sync_policy(
                    wallet=wallet,
                    chain=chain,
                    window_days=window_days,
                    reference_limit=reference_limit,
                    event_limit=event_limit,
                    price_bnb=price_bnb,
                )
                payload.update(
                    {
                        "rebalance_refreshed": True,
                        "rebalance_generated_at": rebalance_result.generated_at,
                        "rebalance_report_path": rebalance_result.report_path,
                        "rebalance_policy_path": rebalance_result.policy_path,
                        "rebalance_policy_entries": rebalance_result.summary.get("policy_entries", 0),
                        "rebalance_top_collection": rebalance_result.summary.get("top_collection"),
                        "rebalance_top_band": rebalance_result.summary.get("top_band"),
                        "rebalance_total_budget_bnb": rebalance_result.summary.get("rebalance_total_budget_bnb", 0.0),
                    }
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Mass-offer rebalance sync failed: %s", exc)
                errors.append(f"rebalance={repr(exc)}")
        if include_quarantine:
            try:
                quarantine_result = self.quarantine.sync_policy(
                    wallet=wallet,
                    chain=chain,
                    window_days=window_days,
                    reference_limit=reference_limit,
                    event_limit=event_limit,
                    price_bnb=price_bnb,
                )
                payload.update(
                    {
                        "quarantine_refreshed": True,
                        "quarantine_generated_at": quarantine_result.generated_at,
                        "quarantine_report_path": quarantine_result.report_path,
                        "quarantine_policy_path": quarantine_result.policy_path,
                        "quarantine_policy_entries": quarantine_result.summary.get("policy_entries", 0),
                        "quarantine_top_collection": quarantine_result.summary.get("top_collection"),
                        "quarantine_top_band": quarantine_result.summary.get("top_band"),
                        "quarantine_earliest_expiry_at": quarantine_result.summary.get("earliest_expiry_at"),
                    }
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Mass-offer quarantine sync failed: %s", exc)
                errors.append(f"quarantine={repr(exc)}")
        if errors and (payload.get("refreshed") or payload.get("budget_refreshed") or payload.get("rebalance_refreshed") or payload.get("quarantine_refreshed")):
            payload["status"] = "partial"
            payload["reason"] = "; ".join(errors)
        elif errors:
            payload["status"] = "error"
            payload["reason"] = "; ".join(errors)
        elif not payload.get("refreshed") and not payload.get("budget_refreshed") and not payload.get("rebalance_refreshed") and not payload.get("quarantine_refreshed"):
            payload["status"] = "skipped"
            payload["reason"] = reason_if_skipped
        return payload

    def _maybe_check_circuit(
        self,
        *,
        enabled: bool,
        wallet: str | None,
        chain: str,
        reason_if_skipped: str,
    ) -> dict[str, Any]:
        if not enabled:
            return {
                "status": "skipped",
                "evaluated": False,
                "reason": reason_if_skipped,
                "should_block_live": False,
            }
        try:
            report = self.circuit.build_report(
                wallet=wallet,
                chain=chain,
                window_hours=self.settings.mass_offer_circuit_window_hours,
            )
            resolved_report_path = self.settings.mass_offer_circuit_report_path
            resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            self.circuit._persist_runtime_summary(report, report_path=resolved_report_path)
        except Exception as exc:  # pragma: no cover - batch execution should remain usable when circuit evaluation fails
            logger.warning("Mass-offer circuit evaluation failed: %s", exc)
            return {
                "status": "error",
                "evaluated": False,
                "reason": repr(exc),
                "should_block_live": False,
            }
        return {
            "status": "ok",
            "evaluated": True,
            "generated_at": report.generated_at,
            "severity": report.summary.get("severity"),
            "issue_code": report.summary.get("issue_code"),
            "should_block_live": bool(report.summary.get("should_block_live")),
            "top_collection": report.summary.get("top_collection"),
            "top_issue_code": report.summary.get("top_issue_code"),
            "recent_live_campaigns": report.summary.get("recent_live_campaigns", 0),
            "recent_submitted_total": report.summary.get("recent_submitted_total", 0),
            "recent_failed_total": report.summary.get("recent_failed_total", 0),
            "recent_blocked_total": report.summary.get("recent_blocked_total", 0),
        }

    def _run_single_collection(
        self,
        *,
        plan_item: MassOfferPlanItem,
        chain: str,
        price_bnb: float,
        dry_run: bool | None,
    ) -> MassOfferBatchCollectionResult:
        requested_dry_run = dry_run
        effective_dry_run = self.engine.governor.effective_dry_run(
            plan_item.dry_run_only or (self.settings.mass_offer_dry_run if dry_run is None else bool(dry_run))
        )
        if not effective_dry_run:
            blocked_reason = self.engine.governor.check_live_submit_allowed(
                action_type="LIVE_MASS_OFFER_BATCH",
                collection=plan_item.collection_key,
                chain=chain,
                price_bnb=price_bnb,
                configured_dry_run=False,
            )
            if blocked_reason:
                return MassOfferBatchCollectionResult(
                    collection_key=plan_item.collection_key,
                    display_name=plan_item.display_name,
                    chain=chain,
                    plan_status=plan_item.status,
                    batch_status="blocked",
                    band=plan_item.band,
                    live_eligible=False,
                    requested_dry_run=requested_dry_run,
                    effective_dry_run=False,
                    planned_offer_count=plan_item.planned_offer_count,
                    planned_exposure_bnb=plan_item.planned_exposure_bnb,
                    recommended_delay_seconds=plan_item.recommended_delay_seconds,
                    blocked_reason=blocked_reason,
                    notes=tuple(dict.fromkeys((*plan_item.notes, "preflight_blocked"))),
                )
        run_result = self.engine.run(
            collection=plan_item.collection_key,
            chain=chain,
            price_bnb=price_bnb,
            max_total=plan_item.planned_offer_count,
            delay_seconds=plan_item.recommended_delay_seconds,
            dry_run=effective_dry_run,
            max_existing_offer=plan_item.max_existing_offer_cap,
            unlisted_only=True,
        )
        batch_status = _classify_run_result(run_result)
        blocked_reason = run_result.blocked_reason
        notes = list(plan_item.notes)
        if run_result.blocked_reason:
            notes.append(f"campaign_blocked:{run_result.blocked_reason}")
        return MassOfferBatchCollectionResult(
            collection_key=plan_item.collection_key,
            display_name=plan_item.display_name,
            chain=chain,
            plan_status=plan_item.status,
            batch_status=batch_status,
            band=plan_item.band,
            live_eligible=plan_item.live_eligible and not effective_dry_run,
            requested_dry_run=requested_dry_run,
            effective_dry_run=run_result.dry_run,
            planned_offer_count=plan_item.planned_offer_count,
            planned_exposure_bnb=plan_item.planned_exposure_bnb,
            recommended_delay_seconds=plan_item.recommended_delay_seconds,
            campaign_id=run_result.campaign_id,
            submitted_count=run_result.submitted_count,
            dry_run_count=run_result.dry_run_count,
            failed_count=run_result.failed_count,
            skipped_count=run_result.skipped_count,
            blocked_reason=blocked_reason,
            notes=tuple(dict.fromkeys(note for note in notes if note)),
        )

    def _persist_runtime_summary(self, report: MassOfferBatchRunReport) -> None:
        self.state.set_runtime_value("last_mass_offer_batch_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_batch_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_batch_requested_dry_run", "1" if report.requested_dry_run is True else ("0" if report.requested_dry_run is False else None))
        self.state.set_runtime_value("last_mass_offer_batch_effective_dry_run", "1" if report.effective_batch_dry_run else "0")
        self.state.set_runtime_value("last_mass_offer_batch_selected_count", report.summary.get("selected_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_executed_live_count", report.summary.get("executed_live_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_executed_dry_run_count", report.summary.get("executed_dry_run_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_submitted_count", report.summary.get("submitted_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_dry_run_offer_count", report.summary.get("dry_run_offer_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_failed_offer_count", report.summary.get("failed_offer_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_remaining_ready_count", report.summary.get("remaining_ready_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_remaining_dry_run_only_count", report.summary.get("remaining_dry_run_only_count", 0))
        self.state.set_runtime_value("last_mass_offer_batch_stopped_reason", report.summary.get("stopped_reason"))
        self.state.set_runtime_value("last_mass_offer_batch_top_collection", report.summary.get("top_selected_collection"))
        self.state.set_runtime_value("last_mass_offer_batch_report_path", report.report_path)
        self._persist_policy_sync_runtime("pre", report.summary.get("pre_policy_sync"))
        self._persist_policy_sync_runtime("post", report.summary.get("post_policy_sync"))
        self._persist_circuit_runtime("pre", report.summary.get("pre_circuit"))
        self._persist_circuit_runtime("mid", report.summary.get("mid_circuit"))
        self._persist_circuit_runtime("post", report.summary.get("post_circuit"))

    def _persist_policy_sync_runtime(self, prefix: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_status", payload.get("status"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_refreshed", "1" if payload.get("refreshed") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_reason", payload.get("reason"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_at", payload.get("generated_at"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_top_collection", payload.get("top_collection"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_policy_sync_top_band", payload.get("top_band"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_allocator_policy_entries", payload.get("allocator_policy_entries", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_feedback_policy_entries", payload.get("feedback_policy_entries", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_sync_refreshed", "1" if payload.get("budget_refreshed") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_policy_entries", payload.get("budget_policy_entries", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_sync_refreshed", "1" if payload.get("rebalance_refreshed") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_policy_entries", payload.get("rebalance_policy_entries", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_generated_at", payload.get("rebalance_generated_at"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_report_path", payload.get("rebalance_report_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_policy_path", payload.get("rebalance_policy_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_top_collection", payload.get("rebalance_top_collection"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_top_band", payload.get("rebalance_top_band"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_rebalance_total_budget_bnb", payload.get("rebalance_total_budget_bnb", 0.0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_generated_at", payload.get("budget_generated_at"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_report_path", payload.get("budget_report_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_policy_path", payload.get("budget_policy_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_top_collection", payload.get("budget_top_collection"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_top_band", payload.get("budget_top_band"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_allocated_total_bnb", payload.get("budget_allocated_total_bnb", 0.0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_budget_allocated_total_slots", payload.get("budget_allocated_total_slots", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_sync_refreshed", "1" if payload.get("quarantine_refreshed") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_policy_entries", payload.get("quarantine_policy_entries", 0))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_generated_at", payload.get("quarantine_generated_at"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_report_path", payload.get("quarantine_report_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_policy_path", payload.get("quarantine_policy_path"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_top_collection", payload.get("quarantine_top_collection"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_top_band", payload.get("quarantine_top_band"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_quarantine_earliest_expiry_at", payload.get("quarantine_earliest_expiry_at"))

    def _persist_circuit_runtime(self, prefix: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_status", payload.get("status"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_evaluated", "1" if payload.get("evaluated") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_reason", payload.get("reason"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_at", payload.get("generated_at"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_severity", payload.get("severity"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_issue_code", payload.get("issue_code"))
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_should_block_live", "1" if payload.get("should_block_live") else "0")
        self.state.set_runtime_value(f"last_mass_offer_batch_{prefix}_circuit_top_collection", payload.get("top_collection"))


def get_mass_offer_batch_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_batch_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_batch_chain"),
        "requested_dry_run": _coerce_optional_bool(runtime.get("last_mass_offer_batch_requested_dry_run")),
        "effective_dry_run": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_effective_dry_run"))),
        "selected_count": _coerce_int(runtime.get("last_mass_offer_batch_selected_count")),
        "executed_live_count": _coerce_int(runtime.get("last_mass_offer_batch_executed_live_count")),
        "executed_dry_run_count": _coerce_int(runtime.get("last_mass_offer_batch_executed_dry_run_count")),
        "submitted_count": _coerce_int(runtime.get("last_mass_offer_batch_submitted_count")),
        "dry_run_offer_count": _coerce_int(runtime.get("last_mass_offer_batch_dry_run_offer_count")),
        "failed_offer_count": _coerce_int(runtime.get("last_mass_offer_batch_failed_offer_count")),
        "remaining_ready_count": _coerce_int(runtime.get("last_mass_offer_batch_remaining_ready_count")),
        "remaining_dry_run_only_count": _coerce_int(runtime.get("last_mass_offer_batch_remaining_dry_run_only_count")),
        "stopped_reason": runtime.get("last_mass_offer_batch_stopped_reason"),
        "top_collection": runtime.get("last_mass_offer_batch_top_collection"),
        "report_path": runtime.get("last_mass_offer_batch_report_path"),
        "pre_sync_status": runtime.get("last_mass_offer_batch_pre_policy_sync_status"),
        "pre_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_pre_policy_sync_refreshed"))),
        "pre_sync_reason": runtime.get("last_mass_offer_batch_pre_policy_sync_reason"),
        "pre_sync_at": runtime.get("last_mass_offer_batch_pre_policy_sync_at"),
        "post_sync_status": runtime.get("last_mass_offer_batch_post_policy_sync_status"),
        "post_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_post_policy_sync_refreshed"))),
        "post_sync_reason": runtime.get("last_mass_offer_batch_post_policy_sync_reason"),
        "post_sync_at": runtime.get("last_mass_offer_batch_post_policy_sync_at"),
        "pre_budget_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_pre_budget_sync_refreshed"))),
        "pre_budget_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_pre_budget_policy_entries")),
        "pre_budget_generated_at": runtime.get("last_mass_offer_batch_pre_budget_generated_at"),
        "pre_budget_top_collection": runtime.get("last_mass_offer_batch_pre_budget_top_collection"),
        "pre_budget_top_band": runtime.get("last_mass_offer_batch_pre_budget_top_band"),
        "pre_budget_allocated_total_bnb": _coerce_float(runtime.get("last_mass_offer_batch_pre_budget_allocated_total_bnb")),
        "post_budget_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_post_budget_sync_refreshed"))),
        "post_budget_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_post_budget_policy_entries")),
        "pre_quarantine_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_pre_quarantine_sync_refreshed"))),
        "pre_quarantine_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_pre_quarantine_policy_entries")),
        "pre_quarantine_generated_at": runtime.get("last_mass_offer_batch_pre_quarantine_generated_at"),
        "pre_quarantine_top_collection": runtime.get("last_mass_offer_batch_pre_quarantine_top_collection"),
        "pre_quarantine_top_band": runtime.get("last_mass_offer_batch_pre_quarantine_top_band"),
        "pre_quarantine_earliest_expiry_at": runtime.get("last_mass_offer_batch_pre_quarantine_earliest_expiry_at"),
        "pre_rebalance_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_pre_rebalance_sync_refreshed"))),
        "pre_rebalance_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_pre_rebalance_policy_entries")),
        "pre_rebalance_generated_at": runtime.get("last_mass_offer_batch_pre_rebalance_generated_at"),
        "pre_rebalance_top_collection": runtime.get("last_mass_offer_batch_pre_rebalance_top_collection"),
        "pre_rebalance_top_band": runtime.get("last_mass_offer_batch_pre_rebalance_top_band"),
        "pre_rebalance_total_budget_bnb": _coerce_float(runtime.get("last_mass_offer_batch_pre_rebalance_total_budget_bnb")),
        "post_budget_generated_at": runtime.get("last_mass_offer_batch_post_budget_generated_at"),
        "post_budget_top_collection": runtime.get("last_mass_offer_batch_post_budget_top_collection"),
        "post_budget_top_band": runtime.get("last_mass_offer_batch_post_budget_top_band"),
        "post_budget_allocated_total_bnb": _coerce_float(runtime.get("last_mass_offer_batch_post_budget_allocated_total_bnb")),
        "post_quarantine_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_post_quarantine_sync_refreshed"))),
        "post_quarantine_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_post_quarantine_policy_entries")),
        "post_quarantine_generated_at": runtime.get("last_mass_offer_batch_post_quarantine_generated_at"),
        "post_quarantine_top_collection": runtime.get("last_mass_offer_batch_post_quarantine_top_collection"),
        "post_quarantine_top_band": runtime.get("last_mass_offer_batch_post_quarantine_top_band"),
        "post_quarantine_earliest_expiry_at": runtime.get("last_mass_offer_batch_post_quarantine_earliest_expiry_at"),
        "post_rebalance_sync_refreshed": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_post_rebalance_sync_refreshed"))),
        "post_rebalance_policy_entries": _coerce_int(runtime.get("last_mass_offer_batch_post_rebalance_policy_entries")),
        "post_rebalance_generated_at": runtime.get("last_mass_offer_batch_post_rebalance_generated_at"),
        "post_rebalance_top_collection": runtime.get("last_mass_offer_batch_post_rebalance_top_collection"),
        "post_rebalance_top_band": runtime.get("last_mass_offer_batch_post_rebalance_top_band"),
        "post_rebalance_total_budget_bnb": _coerce_float(runtime.get("last_mass_offer_batch_post_rebalance_total_budget_bnb")),
        "pre_circuit_status": runtime.get("last_mass_offer_batch_pre_circuit_status"),
        "pre_circuit_severity": runtime.get("last_mass_offer_batch_pre_circuit_severity"),
        "pre_circuit_issue_code": runtime.get("last_mass_offer_batch_pre_circuit_issue_code"),
        "pre_circuit_should_block_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_pre_circuit_should_block_live"))),
        "mid_circuit_status": runtime.get("last_mass_offer_batch_mid_circuit_status"),
        "mid_circuit_severity": runtime.get("last_mass_offer_batch_mid_circuit_severity"),
        "mid_circuit_issue_code": runtime.get("last_mass_offer_batch_mid_circuit_issue_code"),
        "mid_circuit_should_block_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_mid_circuit_should_block_live"))),
        "post_circuit_status": runtime.get("last_mass_offer_batch_post_circuit_status"),
        "post_circuit_severity": runtime.get("last_mass_offer_batch_post_circuit_severity"),
        "post_circuit_issue_code": runtime.get("last_mass_offer_batch_post_circuit_issue_code"),
        "post_circuit_should_block_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_batch_post_circuit_should_block_live"))),
        "circuit_top_collection": runtime.get("last_mass_offer_batch_post_circuit_top_collection") or runtime.get("last_mass_offer_batch_mid_circuit_top_collection") or runtime.get("last_mass_offer_batch_pre_circuit_top_collection"),
        "feedback_top_collection": runtime.get("last_mass_offer_batch_post_policy_sync_top_collection") or runtime.get("last_mass_offer_batch_pre_policy_sync_top_collection"),
        "feedback_top_band": runtime.get("last_mass_offer_batch_post_policy_sync_top_band") or runtime.get("last_mass_offer_batch_pre_policy_sync_top_band"),
        "budget_top_collection": runtime.get("last_mass_offer_batch_post_budget_top_collection") or runtime.get("last_mass_offer_batch_pre_budget_top_collection"),
        "budget_top_band": runtime.get("last_mass_offer_batch_post_budget_top_band") or runtime.get("last_mass_offer_batch_pre_budget_top_band"),
        "budget_allocated_total_bnb": _coerce_float(runtime.get("last_mass_offer_batch_post_budget_allocated_total_bnb") or runtime.get("last_mass_offer_batch_pre_budget_allocated_total_bnb")),
        "rebalance_top_collection": runtime.get("last_mass_offer_batch_post_rebalance_top_collection") or runtime.get("last_mass_offer_batch_pre_rebalance_top_collection"),
        "rebalance_top_band": runtime.get("last_mass_offer_batch_post_rebalance_top_band") or runtime.get("last_mass_offer_batch_pre_rebalance_top_band"),
        "rebalance_total_budget_bnb": _coerce_float(runtime.get("last_mass_offer_batch_post_rebalance_total_budget_bnb") or runtime.get("last_mass_offer_batch_pre_rebalance_total_budget_bnb")),
        "quarantine_top_collection": runtime.get("last_mass_offer_batch_post_quarantine_top_collection") or runtime.get("last_mass_offer_batch_pre_quarantine_top_collection"),
        "quarantine_top_band": runtime.get("last_mass_offer_batch_post_quarantine_top_band") or runtime.get("last_mass_offer_batch_pre_quarantine_top_band"),
        "quarantine_earliest_expiry_at": runtime.get("last_mass_offer_batch_post_quarantine_earliest_expiry_at") or runtime.get("last_mass_offer_batch_pre_quarantine_earliest_expiry_at"),
    }


def format_mass_offer_batch_text(report: MassOfferBatchRunReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_batch",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"price_bnb={report.price_bnb:.6f}",
        f"window_days={report.window_days}",
        (
            f"selected={report.summary.get('selected_count', 0)} live_runs={report.summary.get('executed_live_count', 0)} "
            f"dry_runs={report.summary.get('executed_dry_run_count', 0)} blocked={report.summary.get('blocked_count', 0)} "
            f"failed={report.summary.get('failed_count', 0)}"
        ),
        (
            f"submitted={report.summary.get('submitted_count', 0)} dry_run_offers={report.summary.get('dry_run_offer_count', 0)} "
            f"remaining_ready={report.summary.get('remaining_ready_count', 0)} "
            f"stopped={report.summary.get('stopped_reason', 'n/a')}"
        ),
    ]
    pre_sync = report.summary.get("pre_policy_sync")
    if isinstance(pre_sync, dict):
        lines.append(_format_policy_sync_line("sync_pre", pre_sync))
    post_sync = report.summary.get("post_policy_sync")
    if isinstance(post_sync, dict):
        lines.append(_format_policy_sync_line("sync_post", post_sync))
    pre_circuit = report.summary.get("pre_circuit")
    if isinstance(pre_circuit, dict):
        lines.append(_format_circuit_line("circuit_pre", pre_circuit))
    mid_circuit = report.summary.get("mid_circuit")
    if isinstance(mid_circuit, dict):
        lines.append(_format_circuit_line("circuit_mid", mid_circuit))
    post_circuit = report.summary.get("post_circuit")
    if isinstance(post_circuit, dict):
        lines.append(_format_circuit_line("circuit_post", post_circuit))
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.display_name} [{item.batch_status}|plan={item.plan_status}/{item.band}] | "
                f"offers={item.planned_offer_count} | submitted={item.submitted_count} | dry={item.dry_run_count} | "
                f"failed={item.failed_count} | delay={item.recommended_delay_seconds:.2f}s"
            )
        )
        if item.blocked_reason:
            lines.append(f"  reason={item.blocked_reason}")
    return "\n".join(lines)


def _format_policy_sync_line(label: str, payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "unknown")
    if status != "ok":
        reason = payload.get("reason") or "n/a"
        return f"{label}={status} reason={reason}"
    allocator_entries = int(payload.get("allocator_policy_entries") or 0)
    feedback_entries = int(payload.get("feedback_policy_entries") or 0)
    budget_entries = int(payload.get("budget_policy_entries") or 0)
    quarantine_entries = int(payload.get("quarantine_policy_entries") or 0)
    top_collection = payload.get("top_collection") or "n/a"
    top_band = payload.get("top_band") or "n/a"
    budget_top_collection = payload.get("budget_top_collection") or "n/a"
    budget_top_band = payload.get("budget_top_band") or "n/a"
    budget_allocated = float(payload.get("budget_allocated_total_bnb") or 0.0)
    rebalance_entries = int(payload.get("rebalance_policy_entries") or 0)
    rebalance_top_collection = payload.get("rebalance_top_collection") or "n/a"
    rebalance_top_band = payload.get("rebalance_top_band") or "n/a"
    rebalance_budget = float(payload.get("rebalance_total_budget_bnb") or 0.0)
    quarantine_top_collection = payload.get("quarantine_top_collection") or "n/a"
    quarantine_top_band = payload.get("quarantine_top_band") or "n/a"
    quarantine_expiry = payload.get("quarantine_earliest_expiry_at") or "n/a"
    return (
        f"{label}=ok allocator={allocator_entries} feedback={feedback_entries} budget={budget_entries} rebalance={rebalance_entries} quarantine={quarantine_entries} "
        f"top={top_collection}/{top_band} budget_top={budget_top_collection}/{budget_top_band} "
        f"rebal_top={rebalance_top_collection}/{rebalance_top_band} quarant_top={quarantine_top_collection}/{quarantine_top_band} "
        f"budget_alloc={budget_allocated:.4f} rebal_alloc={rebalance_budget:.4f} quarantine_next={quarantine_expiry}"
    )


def _format_circuit_line(label: str, payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "unknown")
    if status != "ok":
        reason = payload.get("reason") or "n/a"
        return f"{label}={status} reason={reason}"
    severity = str(payload.get("severity") or "ok")
    issue_code = payload.get("issue_code") or "none"
    top_collection = payload.get("top_collection") or "n/a"
    block_live = bool(payload.get("should_block_live"))
    return f"{label}=ok severity={severity} issue={issue_code} block={block_live} top={top_collection}"


def _pick_next_candidate(
    items: list[MassOfferPlanItem],
    *,
    seen: set[str],
    include_dry_run_collections: bool,
) -> MassOfferPlanItem | None:
    allowed = {"ready"}
    if include_dry_run_collections:
        allowed.add("dry_run_only")
    for item in items:
        if item.collection_key in seen:
            continue
        if item.status in allowed:
            return item
    return None


def _classify_run_result(result: MassOfferRunResult) -> str:
    if result.blocked_reason:
        return "blocked"
    if result.dry_run:
        if result.dry_run_count > 0:
            return "executed_dry_run"
        if result.failed_count > 0:
            return "failed"
        return "blocked"
    if result.submitted_count > 0 and result.failed_count > 0:
        return "partial"
    if result.submitted_count > 0:
        return "executed_live"
    if result.failed_count > 0:
        return "failed"
    if any(item.status == "blocked" for item in result.results):
        return "blocked"
    return "blocked"


def _is_global_live_block(reason: str) -> bool:
    prefixes = (
        "rate limit hit:",
        "daily BNB limit hit:",
        "submit cooldown active:",
        "portfolio_risk:",
        "killswitch/integrity:",
        "live arm",
        "dry_run_enabled",
    )
    return any(reason.startswith(prefix) for prefix in prefixes)



def _coerce_float(value: Any) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0

def _coerce_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None
