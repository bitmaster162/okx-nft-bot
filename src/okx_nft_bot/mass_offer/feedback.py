from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.allocator import (
    CollectionAllocationRecommendation,
    MassOfferAllocator,
    MassOfferAllocatorReport,
)
from okx_nft_bot.mass_offer.tracker import MassOfferCampaign, MassOfferTracker
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


@dataclass(slots=True)
class CollectionFeedbackRecommendation:
    collection_key: str
    display_name: str
    chain: str
    feedback_band: str
    efficiency_score: float
    confidence: float
    enabled: bool
    dry_run_only: bool
    preferred_max_total: int | None
    max_total_cap: int | None
    preferred_delay_seconds: float | None
    min_delay_seconds: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    live_campaigns: int = 0
    dry_run_campaigns: int = 0
    completed_campaigns: int = 0
    target_total: int = 0
    submitted_total: int = 0
    failed_total: int = 0
    skipped_total: int = 0
    blocked_submit_total: int = 0
    no_submit_live_campaigns: int = 0
    target_utilization: float = 0.0
    submit_success_rate: float = 0.0
    failed_ratio: float = 0.0
    blocked_ratio: float = 0.0
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    oldest_active_offer_hours: float | None = None
    last_campaign_at: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[float, float, str]:
        return (-self.efficiency_score, -self.confidence, self.display_name.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "feedback_band": self.feedback_band,
            "efficiency_score": round(self.efficiency_score, 3),
            "confidence": round(self.confidence, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "live_campaigns": self.live_campaigns,
            "dry_run_campaigns": self.dry_run_campaigns,
            "completed_campaigns": self.completed_campaigns,
            "target_total": self.target_total,
            "submitted_total": self.submitted_total,
            "failed_total": self.failed_total,
            "skipped_total": self.skipped_total,
            "blocked_submit_total": self.blocked_submit_total,
            "no_submit_live_campaigns": self.no_submit_live_campaigns,
            "target_utilization": round(self.target_utilization, 4),
            "submit_success_rate": round(self.submit_success_rate, 4),
            "failed_ratio": round(self.failed_ratio, 4),
            "blocked_ratio": round(self.blocked_ratio, 4),
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "oldest_active_offer_hours": round(self.oldest_active_offer_hours, 2) if self.oldest_active_offer_hours is not None else None,
            "last_campaign_at": self.last_campaign_at,
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
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "source": "feedback_report",
            "feedback_band": self.feedback_band,
            "feedback_score": round(self.efficiency_score, 3),
            "feedback_confidence": round(self.confidence, 4),
            "notes": list(self.notes),
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class MassOfferFeedbackReport:
    generated_at: str
    wallet: str | None
    chain: str
    window_days: int
    report_path: str
    policy_path: str
    allocator: MassOfferAllocatorReport
    summary: dict[str, Any]
    collections: list[CollectionFeedbackRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "window_days": self.window_days,
            "report_path": self.report_path,
            "policy_path": self.policy_path,
            "allocator": self.allocator.to_dict(),
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
class MassOfferPolicyOverlaySyncResult:
    generated_at: str
    wallet: str | None
    chain: str
    window_days: int
    allocator_report_path: str
    allocator_policy_path: str
    feedback_report_path: str
    feedback_policy_path: str
    allocator_summary: dict[str, Any]
    feedback_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "window_days": self.window_days,
            "allocator_report_path": self.allocator_report_path,
            "allocator_policy_path": self.allocator_policy_path,
            "feedback_report_path": self.feedback_report_path,
            "feedback_policy_path": self.feedback_policy_path,
            "allocator_summary": self.allocator_summary,
            "feedback_summary": self.feedback_summary,
        }


class MassOfferFeedbackController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
        tracker: MassOfferTracker | None = None,
        allocator: MassOfferAllocator | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.tracker = tracker or MassOfferTracker(settings.execution_db_path)
        self.allocator = allocator or MassOfferAllocator(settings=settings, store=self.store)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
    ) -> MassOfferFeedbackReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_feedback_window_days)
        allocator_report = self.allocator.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
        )
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=max(resolved_window_days, 1))
        recent_campaigns = self.tracker.list_campaigns_since(chain=resolved_chain, since=since)
        campaign_stats = _aggregate_recent_campaigns(recent_campaigns)
        submit_stats = {
            str(row.get("collection") or "").strip().lower(): row
            for row in self.state.get_collection_submit_stats(chain=resolved_chain, since=since)
        }
        active_stats = {
            str(row.get("collection") or "").strip().lower(): row
            for row in self.state.get_collection_active_stats(chain=resolved_chain)
        }

        feedback: list[CollectionFeedbackRecommendation] = []
        for recommendation in allocator_report.collections:
            key = recommendation.collection_key
            campaign = campaign_stats.get(key, {})
            submit = submit_stats.get(key, {})
            active = active_stats.get(key, {})
            feedback.append(
                _build_feedback_recommendation(
                    settings=self.settings,
                    recommendation=recommendation,
                    campaign=campaign,
                    submit=submit,
                    active=active,
                )
            )
        feedback.sort(key=lambda item: item.sort_key())
        summary = {
            "collection_count": len(feedback),
            "policy_entries": len(feedback),
            "promote_count": sum(1 for item in feedback if item.feedback_band == "promote"),
            "steady_count": sum(1 for item in feedback if item.feedback_band == "steady"),
            "throttle_count": sum(1 for item in feedback if item.feedback_band == "throttle"),
            "watch_count": sum(1 for item in feedback if item.feedback_band == "watch"),
            "pause_count": sum(1 for item in feedback if item.feedback_band == "pause"),
            "live_enabled_count": sum(1 for item in feedback if item.enabled and not item.dry_run_only),
            "dry_run_only_count": sum(1 for item in feedback if item.enabled and item.dry_run_only),
            "top_collection": feedback[0].collection_key if feedback else None,
            "top_band": feedback[0].feedback_band if feedback else None,
            "top_score": round(feedback[0].efficiency_score, 3) if feedback else 0.0,
            "recent_live_campaigns": sum(item.live_campaigns for item in feedback),
            "recent_submitted_total": sum(item.submitted_total for item in feedback),
        }
        return MassOfferFeedbackReport(
            generated_at=now.isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            window_days=max(resolved_window_days, 1),
            report_path=str(self.settings.mass_offer_feedback_report_path),
            policy_path=str(self.settings.mass_offer_feedback_policy_path),
            allocator=allocator_report,
            summary=summary,
            collections=feedback,
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
        resolved_report_path = report_path or self.settings.mass_offer_feedback_report_path
        _write_json(resolved_report_path, report.to_dict())
        resolved_policy_path = policy_path or self.settings.mass_offer_feedback_policy_path
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(
            report,
            report_path=resolved_report_path,
            policy_path=resolved_policy_path,
        )
        return {
            "report_path": str(resolved_report_path),
            "policy_path": str(resolved_policy_path),
        }

    def sync_policy_bundle(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        allocator_report_path: Path | None = None,
        allocator_policy_path: Path | None = None,
        feedback_report_path: Path | None = None,
        feedback_policy_path: Path | None = None,
        allocator_limit: int | None = None,
        feedback_limit: int | None = None,
    ) -> MassOfferPolicyOverlaySyncResult:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
        )
        resolved_allocator_report_path = allocator_report_path or self.settings.mass_offer_allocator_report_path
        resolved_allocator_policy_path = allocator_policy_path or self.settings.mass_offer_allocator_policy_path
        resolved_feedback_report_path = feedback_report_path or self.settings.mass_offer_feedback_report_path
        resolved_feedback_policy_path = feedback_policy_path or self.settings.mass_offer_feedback_policy_path

        _write_json(resolved_allocator_report_path, report.allocator.to_dict())
        _write_json(resolved_allocator_policy_path, report.allocator.to_policy_overrides(limit=allocator_limit))
        _write_json(resolved_feedback_report_path, report.to_dict())
        _write_json(resolved_feedback_policy_path, report.to_policy_overrides(limit=feedback_limit))

        self._persist_runtime_summary(
            report,
            report_path=resolved_feedback_report_path,
            policy_path=resolved_feedback_policy_path,
            allocator_report_path=resolved_allocator_report_path,
            allocator_policy_path=resolved_allocator_policy_path,
        )
        return MassOfferPolicyOverlaySyncResult(
            generated_at=report.generated_at,
            wallet=report.wallet,
            chain=report.chain,
            window_days=report.window_days,
            allocator_report_path=str(resolved_allocator_report_path),
            allocator_policy_path=str(resolved_allocator_policy_path),
            feedback_report_path=str(resolved_feedback_report_path),
            feedback_policy_path=str(resolved_feedback_policy_path),
            allocator_summary=report.allocator.summary,
            feedback_summary=report.summary,
        )

    def _persist_runtime_summary(
        self,
        report: MassOfferFeedbackReport,
        *,
        report_path: Path,
        policy_path: Path,
        allocator_report_path: Path | None = None,
        allocator_policy_path: Path | None = None,
    ) -> None:
        self.state.set_runtime_value("last_mass_offer_feedback_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_feedback_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_feedback_window_days", report.window_days)
        self.state.set_runtime_value("last_mass_offer_feedback_report_path", str(report_path))
        self.state.set_runtime_value("last_mass_offer_feedback_policy_path", str(policy_path))
        if allocator_report_path is not None:
            self.state.set_runtime_value("last_mass_offer_allocator_report_path", str(allocator_report_path))
        if allocator_policy_path is not None:
            self.state.set_runtime_value("last_mass_offer_allocator_policy_path", str(allocator_policy_path))
        self.state.set_runtime_value("last_mass_offer_feedback_policy_entries", report.summary.get("policy_entries", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_promote_count", report.summary.get("promote_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_steady_count", report.summary.get("steady_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_throttle_count", report.summary.get("throttle_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_watch_count", report.summary.get("watch_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_pause_count", report.summary.get("pause_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_live_enabled_count", report.summary.get("live_enabled_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_dry_run_only_count", report.summary.get("dry_run_only_count", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_recent_live_campaigns", report.summary.get("recent_live_campaigns", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_recent_submitted_total", report.summary.get("recent_submitted_total", 0))
        self.state.set_runtime_value("last_mass_offer_feedback_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_feedback_top_band", report.summary.get("top_band"))


def apply_feedback_to_recommendation(
    recommendation: CollectionAllocationRecommendation,
    feedback: CollectionFeedbackRecommendation | None,
) -> CollectionAllocationRecommendation:
    if feedback is None:
        return recommendation

    baseline_cap = recommendation.max_total_cap
    preferred_max_total = feedback.preferred_max_total
    max_total_cap = feedback.max_total_cap
    if baseline_cap is not None and baseline_cap > 0:
        if preferred_max_total is not None:
            preferred_max_total = min(int(preferred_max_total), int(baseline_cap))
        if max_total_cap is not None:
            max_total_cap = min(int(max_total_cap), int(baseline_cap))
    notes = tuple(
        dict.fromkeys(
            [
                *recommendation.notes,
                *feedback.notes,
                f"feedback_band={feedback.feedback_band}",
                f"feedback_score={feedback.efficiency_score:.1f}",
            ]
        )
    )
    return replace(
        recommendation,
        enabled=feedback.enabled,
        dry_run_only=feedback.dry_run_only,
        preferred_max_total=preferred_max_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=feedback.preferred_delay_seconds,
        min_delay_seconds=feedback.min_delay_seconds,
        max_active_offers=feedback.max_active_offers,
        max_active_exposure_bnb=feedback.max_active_exposure_bnb,
        notes=notes,
    )


def format_mass_offer_feedback_text(report: MassOfferFeedbackReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_feedback",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"window_days={report.window_days}",
        (
            f"promote={report.summary.get('promote_count', 0)} steady={report.summary.get('steady_count', 0)} "
            f"throttle={report.summary.get('throttle_count', 0)} watch={report.summary.get('watch_count', 0)} "
            f"pause={report.summary.get('pause_count', 0)}"
        ),
        (
            f"live_enabled={report.summary.get('live_enabled_count', 0)} dry_run_only={report.summary.get('dry_run_only_count', 0)} "
            f"recent_live_campaigns={report.summary.get('recent_live_campaigns', 0)} recent_submitted={report.summary.get('recent_submitted_total', 0)}"
        ),
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.display_name} [{item.feedback_band}] | score={item.efficiency_score:.2f} | conf={item.confidence:.2f} | "
                f"util={item.target_utilization:.2f} | success={item.submit_success_rate:.2f} | failed={item.failed_ratio:.2f} | "
                f"offers={item.preferred_max_total} | delay={item.preferred_delay_seconds if item.preferred_delay_seconds is not None else 'n/a'}"
            )
        )
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _aggregate_recent_campaigns(campaigns: list[MassOfferCampaign]) -> dict[str, dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for campaign in campaigns:
        key = campaign.collection.strip().lower()
        bucket = aggregated.setdefault(
            key,
            {
                "live_campaigns": 0,
                "dry_run_campaigns": 0,
                "completed_campaigns": 0,
                "target_total": 0,
                "submitted_total": 0,
                "failed_total": 0,
                "skipped_total": 0,
                "no_submit_live_campaigns": 0,
                "last_campaign_at": None,
            },
        )
        if campaign.dry_run:
            bucket["dry_run_campaigns"] += 1
        else:
            bucket["live_campaigns"] += 1
            bucket["target_total"] += int(campaign.target_count)
            bucket["submitted_total"] += int(campaign.submitted_count)
            bucket["failed_total"] += int(campaign.failed_count)
            bucket["skipped_total"] += int(campaign.skipped_count)
            if int(campaign.submitted_count) <= 0:
                bucket["no_submit_live_campaigns"] += 1
        if campaign.status == "completed":
            bucket["completed_campaigns"] += 1
        last_campaign_at = bucket.get("last_campaign_at")
        if last_campaign_at is None or campaign.updated_at.isoformat() > str(last_campaign_at):
            bucket["last_campaign_at"] = campaign.updated_at.isoformat()
    return aggregated


def _build_feedback_recommendation(
    *,
    settings: Settings,
    recommendation: CollectionAllocationRecommendation,
    campaign: dict[str, Any],
    submit: dict[str, Any],
    active: dict[str, Any],
) -> CollectionFeedbackRecommendation:
    notes: list[str] = [f"allocator_band={recommendation.band}"]
    live_campaigns = int(campaign.get("live_campaigns", 0) or 0)
    dry_run_campaigns = int(campaign.get("dry_run_campaigns", 0) or 0)
    completed_campaigns = int(campaign.get("completed_campaigns", 0) or 0)
    target_total = int(campaign.get("target_total", 0) or 0)
    submitted_total = int(campaign.get("submitted_total", 0) or 0)
    failed_total = int(campaign.get("failed_total", 0) or 0)
    skipped_total = int(campaign.get("skipped_total", 0) or 0)
    no_submit_live_campaigns = int(campaign.get("no_submit_live_campaigns", 0) or 0)
    blocked_submit_total = int(submit.get("blocked_count", 0) or 0)
    submit_success_total = int(submit.get("submitted_count", 0) or 0)
    submit_failed_total = int(submit.get("failed_count", 0) or 0)

    active_offer_count = int(active.get("active_offer_count", 0) or 0)
    active_exposure_bnb = float(active.get("active_exposure_bnb", 0.0) or 0.0)
    oldest_active_offer_hours = _optional_float(active.get("oldest_active_offer_hours"))

    target_utilization = submitted_total / target_total if target_total > 0 else 0.0
    submit_attempts = submit_success_total + submit_failed_total + blocked_submit_total
    submit_success_rate = submit_success_total / submit_attempts if submit_attempts > 0 else 0.0
    failed_ratio = failed_total / max(target_total, 1) if target_total > 0 else (1.0 if live_campaigns > 0 and failed_total > 0 else 0.0)
    blocked_ratio = blocked_submit_total / submit_attempts if submit_attempts > 0 else 0.0

    score = 0.0
    if recommendation.band == "overweight":
        score += 5.0
    elif recommendation.band == "neutral":
        score += 2.0
    elif recommendation.band == "underweight":
        score -= 4.0
    elif recommendation.band == "watch":
        score -= 10.0
    elif recommendation.band == "block":
        score -= 18.0

    if recommendation.dry_run_only:
        score -= 4.0
        notes.append("baseline_dry_run_only")

    if live_campaigns <= 0:
        score -= 2.0
        notes.append("no_recent_live_campaigns")
    else:
        score += min(live_campaigns * 2.0, 8.0)
        if target_utilization >= 0.75:
            score += 18.0
            notes.append(f"strong_target_utilization={target_utilization:.2f}")
        elif target_utilization >= 0.50:
            score += 8.0
            notes.append(f"healthy_target_utilization={target_utilization:.2f}")
        elif target_total > 0 and target_utilization < 0.25:
            score -= 18.0
            notes.append(f"weak_target_utilization={target_utilization:.2f}")
        elif target_total > 0 and target_utilization < 0.45:
            score -= 8.0
            notes.append(f"soft_target_utilization={target_utilization:.2f}")

        if no_submit_live_campaigns >= 2:
            score -= 10.0
            notes.append(f"no_submit_live_campaigns={no_submit_live_campaigns}")
        elif no_submit_live_campaigns == 1 and live_campaigns >= 3:
            score -= 4.0
            notes.append("single_empty_live_campaign")

    if submit_attempts > 0:
        if submit_success_rate >= 0.80:
            score += 14.0
            notes.append(f"strong_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate >= 0.60:
            score += 6.0
            notes.append(f"healthy_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate < 0.40:
            score -= 14.0
            notes.append(f"weak_submit_success={submit_success_rate:.2f}")
        elif submit_success_rate < 0.55:
            score -= 6.0
            notes.append(f"soft_submit_success={submit_success_rate:.2f}")

        if blocked_ratio >= 0.40:
            score -= 14.0
            notes.append(f"high_blocked_ratio={blocked_ratio:.2f}")
        elif blocked_ratio >= 0.20:
            score -= 6.0
            notes.append(f"elevated_blocked_ratio={blocked_ratio:.2f}")
        elif blocked_ratio == 0 and submit_success_total > 0:
            score += 3.0
            notes.append("no_recent_blocks")
    else:
        notes.append("no_recent_submit_events")

    if failed_ratio >= 0.35:
        score -= 16.0
        notes.append(f"high_failed_ratio={failed_ratio:.2f}")
    elif failed_ratio >= 0.15:
        score -= 7.0
        notes.append(f"elevated_failed_ratio={failed_ratio:.2f}")
    elif live_campaigns >= 2 and failed_ratio <= 0.05:
        score += 4.0
        notes.append("low_failed_ratio")

    if active_offer_count > 0:
        if oldest_active_offer_hours is not None and oldest_active_offer_hours >= 96.0:
            score -= 26.0
            notes.append(f"very_stale_active_offers={oldest_active_offer_hours:.1f}h")
        elif oldest_active_offer_hours is not None and oldest_active_offer_hours >= 48.0:
            score -= 16.0
            notes.append(f"stale_active_offers={oldest_active_offer_hours:.1f}h")
        elif oldest_active_offer_hours is not None and oldest_active_offer_hours >= 24.0:
            score -= 8.0
            notes.append(f"aging_active_offers={oldest_active_offer_hours:.1f}h")
        else:
            score += 2.0
            notes.append("fresh_active_book")
    elif live_campaigns > 0 and submitted_total > 0:
        score += 3.0
        notes.append("no_current_active_book")

    if recommendation.max_active_exposure_bnb is not None and active_exposure_bnb > float(recommendation.max_active_exposure_bnb) + 1e-12:
        score -= 10.0
        notes.append(
            f"active_exposure_above_allocator_cap={active_exposure_bnb:.3f}>{float(recommendation.max_active_exposure_bnb):.3f}"
        )

    if recommendation.open_position_count > 0:
        score -= min(recommendation.open_position_count * 1.5, 8.0)
        notes.append(f"open_positions={recommendation.open_position_count}")
    if recommendation.orphan_sale_count > 0:
        score -= min(recommendation.orphan_sale_count * 6.0, 18.0)
        notes.append(f"orphan_sales={recommendation.orphan_sale_count}")

    score = _clamp(score, -100.0, 100.0)
    confidence = 0.15
    if live_campaigns > 0:
        confidence += min(live_campaigns * 0.08, 0.28)
    if submit_attempts > 0:
        confidence += 0.18
    if target_total >= 5:
        confidence += 0.08
    if active_offer_count > 0:
        confidence += 0.08
    if recommendation.recent_sales_count and recommendation.recent_sales_count > 0:
        confidence += 0.08
    confidence = _clamp(confidence, 0.15, 0.95)

    if score >= 22.0:
        band = "promote"
    elif score >= -5.0:
        band = "steady"
    elif score >= -30.0:
        band = "throttle"
    elif score >= -55.0:
        band = "watch"
    else:
        band = "pause"

    base_total = max(int(recommendation.preferred_max_total or recommendation.max_total_cap or 1), 1)
    base_delay = float(recommendation.preferred_delay_seconds or recommendation.min_delay_seconds or settings.mass_offer_delay_seconds)
    base_active_offers = recommendation.max_active_offers or max(min(base_total, 5), 1)
    base_active_exposure = recommendation.max_active_exposure_bnb

    if band == "promote":
        enabled = recommendation.enabled
        dry_run_only = recommendation.dry_run_only
        preferred_max_total = _clamp_int(round(base_total * 1.25 + (1 if live_campaigns >= 3 else 0)), 1, int(settings.mass_offer_max_total))
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 0.85, 0.5)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = max(base_active_offers, min(preferred_max_total, 6))
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure, active_exposure_bnb, 1.15)
    elif band == "steady":
        enabled = recommendation.enabled
        dry_run_only = recommendation.dry_run_only
        preferred_max_total = base_total
        max_total_cap = recommendation.max_total_cap or preferred_max_total
        preferred_delay_seconds = max(base_delay, 0.5)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = base_active_offers
        max_active_exposure_bnb = base_active_exposure
    elif band == "throttle":
        enabled = True
        dry_run_only = recommendation.dry_run_only or score < -15.0
        preferred_max_total = _clamp_int(round(base_total * 0.7), 1, int(settings.mass_offer_max_total))
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 1.35, 1.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = max(1, min(base_active_offers, preferred_max_total))
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure, active_exposure_bnb, 0.75)
    elif band == "watch":
        enabled = True
        dry_run_only = True
        preferred_max_total = 1 if submitted_total <= 0 else min(2, base_total)
        max_total_cap = preferred_max_total
        preferred_delay_seconds = max(base_delay * 2.0, 2.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure, active_exposure_bnb, 0.45)
    else:
        enabled = False
        dry_run_only = True
        preferred_max_total = 1
        max_total_cap = 1
        preferred_delay_seconds = max(base_delay * 4.0, 4.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = _scale_exposure_cap(base_active_exposure, active_exposure_bnb, 0.25)

    if dry_run_only:
        notes.append("feedback_forces_dry_run")
    if not enabled:
        notes.append("feedback_pauses_collection")
    notes.extend(
        [
            f"feedback_band={band}",
            f"feedback_preferred_total={preferred_max_total}",
            f"feedback_preferred_delay={preferred_delay_seconds:.2f}s",
        ]
    )

    return CollectionFeedbackRecommendation(
        collection_key=recommendation.collection_key,
        display_name=recommendation.display_name,
        chain=recommendation.chain,
        feedback_band=band,
        efficiency_score=score,
        confidence=confidence,
        enabled=enabled,
        dry_run_only=dry_run_only,
        preferred_max_total=preferred_max_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=preferred_delay_seconds,
        min_delay_seconds=min_delay_seconds,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        live_campaigns=live_campaigns,
        dry_run_campaigns=dry_run_campaigns,
        completed_campaigns=completed_campaigns,
        target_total=target_total,
        submitted_total=submitted_total,
        failed_total=failed_total,
        skipped_total=skipped_total,
        blocked_submit_total=blocked_submit_total,
        no_submit_live_campaigns=no_submit_live_campaigns,
        target_utilization=target_utilization,
        submit_success_rate=submit_success_rate,
        failed_ratio=failed_ratio,
        blocked_ratio=blocked_ratio,
        current_active_offers=active_offer_count,
        current_active_exposure_bnb=active_exposure_bnb,
        oldest_active_offer_hours=oldest_active_offer_hours,
        last_campaign_at=campaign.get("last_campaign_at"),
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _scale_exposure_cap(base_cap: float | None, current_exposure: float, multiplier: float) -> float | None:
    target = None
    if base_cap is not None and base_cap > 0:
        target = float(base_cap) * float(multiplier)
    elif current_exposure > 0:
        target = float(current_exposure) * max(float(multiplier), 0.5)
    if target is None or target <= 0:
        return None
    if current_exposure > 0:
        target = max(target, current_exposure)
    return round(target, 6)


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    return float(value)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))
