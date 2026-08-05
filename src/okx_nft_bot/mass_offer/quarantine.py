from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.rebalancer import (
    CollectionBudgetRebalanceRecommendation,
    MassOfferBudgetRebalancer,
    MassOfferRebalanceReport,
)
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_BAND_ORDER = {
    "block": 0,
    "dry_run": 1,
    "cooldown": 2,
    "clear": 3,
}


@dataclass(slots=True)
class CollectionQuarantineRecommendation:
    collection_key: str
    display_name: str
    chain: str
    quarantine_band: str
    quarantine_score: float
    confidence: float
    enabled: bool
    dry_run_only: bool
    expires_at: str | None
    preferred_max_total: int | None
    max_total_cap: int | None
    preferred_delay_seconds: float | None
    min_delay_seconds: float | None
    max_active_offers: int | None
    max_active_exposure_bnb: float | None
    rebalance_band: str | None = None
    feedback_band: str | None = None
    allocation_band: str | None = None
    live_campaigns: int = 0
    submitted_total: int = 0
    target_total: int = 0
    failed_ratio: float = 0.0
    blocked_ratio: float = 0.0
    no_submit_live_campaigns: int = 0
    current_active_offers: int = 0
    current_active_exposure_bnb: float = 0.0
    rebalance_budget_bnb: float = 0.0
    realized_roi_pct: float | None = None
    unrealized_roi_pct: float | None = None
    blocked_reason: str | None = None
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, float, float, str]:
        return (
            _BAND_ORDER.get(self.quarantine_band, 99),
            self._hours_remaining_sort(),
            self.quarantine_score,
            self.display_name.lower(),
        )

    def _hours_remaining_sort(self) -> float:
        remaining = _hours_remaining(self.expires_at)
        return remaining if remaining is not None else 9_999.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "display_name": self.display_name,
            "chain": self.chain,
            "quarantine_band": self.quarantine_band,
            "quarantine_score": round(self.quarantine_score, 3),
            "confidence": round(self.confidence, 4),
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "expires_at": self.expires_at,
            "hours_remaining": round(_hours_remaining(self.expires_at), 2) if _hours_remaining(self.expires_at) is not None else None,
            "preferred_max_total": self.preferred_max_total,
            "max_total_cap": self.max_total_cap,
            "preferred_delay_seconds": round(self.preferred_delay_seconds, 6) if self.preferred_delay_seconds is not None else None,
            "min_delay_seconds": round(self.min_delay_seconds, 6) if self.min_delay_seconds is not None else None,
            "max_active_offers": self.max_active_offers,
            "max_active_exposure_bnb": round(self.max_active_exposure_bnb, 6) if self.max_active_exposure_bnb is not None else None,
            "rebalance_band": self.rebalance_band,
            "feedback_band": self.feedback_band,
            "allocation_band": self.allocation_band,
            "live_campaigns": self.live_campaigns,
            "submitted_total": self.submitted_total,
            "target_total": self.target_total,
            "failed_ratio": round(self.failed_ratio, 4),
            "blocked_ratio": round(self.blocked_ratio, 4),
            "no_submit_live_campaigns": self.no_submit_live_campaigns,
            "current_active_offers": self.current_active_offers,
            "current_active_exposure_bnb": round(self.current_active_exposure_bnb, 6),
            "rebalance_budget_bnb": round(self.rebalance_budget_bnb, 6),
            "realized_roi_pct": round(self.realized_roi_pct, 4) if self.realized_roi_pct is not None else None,
            "unrealized_roi_pct": round(self.unrealized_roi_pct, 4) if self.unrealized_roi_pct is not None else None,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.notes),
        }

    def to_policy_override(self) -> dict[str, Any] | None:
        if self.quarantine_band == "clear":
            return None
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
            "expires_at": self.expires_at,
            "source": "quarantine_report",
            "quarantine_band": self.quarantine_band,
            "quarantine_score": round(self.quarantine_score, 3),
            "quarantine_confidence": round(self.confidence, 4),
            "notes": list(self.notes),
        }
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class MassOfferQuarantineReport:
    generated_at: str
    wallet: str | None
    chain: str
    price_bnb: float
    window_days: int
    report_path: str
    policy_path: str
    rebalancer_summary: dict[str, Any]
    summary: dict[str, Any]
    collections: list[CollectionQuarantineRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": round(self.price_bnb, 6),
            "window_days": self.window_days,
            "report_path": self.report_path,
            "policy_path": self.policy_path,
            "rebalancer_summary": self.rebalancer_summary,
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }

    def to_policy_overrides(self, *, limit: int | None = None) -> dict[str, Any]:
        items = self.collections[:limit] if limit is not None else self.collections
        payload_items = {
            item.collection_key: override
            for item in items
            if (override := item.to_policy_override()) is not None
        }
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "price_bnb": self.price_bnb,
            "window_days": self.window_days,
            "summary": self.summary,
            "collections": payload_items,
        }


@dataclass(slots=True)
class MassOfferQuarantineSyncResult:
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


class MassOfferQuarantineController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore | None = None,
        state: PositionState | None = None,
        rebalancer: MassOfferBudgetRebalancer | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteStore(settings.db_path)
        self.state = state or PositionState(settings.execution_db_path)
        self.rebalancer = rebalancer or MassOfferBudgetRebalancer(
            settings=settings,
            store=self.store,
            state=self.state,
        )

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_days: int | None = None,
        reference_limit: int | None = None,
        event_limit: int | None = None,
        price_bnb: float | None = None,
    ) -> MassOfferQuarantineReport:
        resolved_chain = chain.strip().lower()
        resolved_window_days = int(window_days if window_days is not None else self.settings.mass_offer_quarantine_window_days)
        resolved_price_bnb = float(price_bnb if price_bnb is not None else self.settings.mass_offer_price_bnb)
        rebalancer_report = self.rebalancer.build_report(
            wallet=wallet,
            chain=resolved_chain,
            window_days=resolved_window_days,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            event_limit=event_limit or self.settings.mass_offer_economics_event_limit,
            price_bnb=resolved_price_bnb,
        )
        now = datetime.now(timezone.utc)
        collections = [
            _build_quarantine_recommendation(
                settings=self.settings,
                recommendation=item,
                price_bnb=resolved_price_bnb,
                now=now,
            )
            for item in rebalancer_report.collections
        ]
        collections.sort(key=lambda item: item.sort_key())
        active_items = [item for item in collections if item.quarantine_band != "clear"]
        top_item = active_items[0] if active_items else None
        expiry_candidates = [item.expires_at for item in active_items if item.expires_at]
        summary = {
            "collection_count": len(collections),
            "policy_entries": len(active_items),
            "clear_count": sum(1 for item in collections if item.quarantine_band == "clear"),
            "cooldown_count": sum(1 for item in collections if item.quarantine_band == "cooldown"),
            "dry_run_count": sum(1 for item in collections if item.quarantine_band == "dry_run"),
            "block_count": sum(1 for item in collections if item.quarantine_band == "block"),
            "active_quarantine_count": len(active_items),
            "live_throttle_count": sum(1 for item in active_items if item.enabled and not item.dry_run_only),
            "dry_run_only_count": sum(1 for item in active_items if item.enabled and item.dry_run_only),
            "blocked_count": sum(1 for item in active_items if not item.enabled),
            "top_collection": top_item.collection_key if top_item is not None else None,
            "top_band": top_item.quarantine_band if top_item is not None else None,
            "top_score": round(top_item.quarantine_score, 3) if top_item is not None else 0.0,
            "top_expiry_at": top_item.expires_at if top_item is not None else None,
            "earliest_expiry_at": min(expiry_candidates) if expiry_candidates else None,
            "rebalancer_policy_entries": rebalancer_report.summary.get("policy_entries", 0),
            "rebalancer_top_collection": rebalancer_report.summary.get("top_collection"),
            "rebalancer_top_band": rebalancer_report.summary.get("top_band"),
        }
        return MassOfferQuarantineReport(
            generated_at=now.isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            price_bnb=resolved_price_bnb,
            window_days=resolved_window_days,
            report_path=str(self.settings.mass_offer_quarantine_report_path),
            policy_path=str(self.settings.mass_offer_quarantine_policy_path),
            rebalancer_summary=rebalancer_report.summary,
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
    ) -> str:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_quarantine_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_quarantine_policy_path
        _write_json(resolved_report_path, report.to_dict())
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(report, report_path=resolved_report_path, policy_path=resolved_policy_path)
        return str(resolved_report_path)

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
    ) -> MassOfferQuarantineSyncResult:
        report = self.build_report(
            wallet=wallet,
            chain=chain,
            window_days=window_days,
            reference_limit=reference_limit,
            event_limit=event_limit,
            price_bnb=price_bnb,
        )
        resolved_report_path = report_path or self.settings.mass_offer_quarantine_report_path
        resolved_policy_path = policy_path or self.settings.mass_offer_quarantine_policy_path
        _write_json(resolved_report_path, report.to_dict())
        _write_json(resolved_policy_path, report.to_policy_overrides(limit=limit))
        self._persist_runtime_summary(report, report_path=resolved_report_path, policy_path=resolved_policy_path)
        return MassOfferQuarantineSyncResult(
            generated_at=report.generated_at,
            wallet=report.wallet,
            chain=report.chain,
            window_days=report.window_days,
            report_path=str(resolved_report_path),
            policy_path=str(resolved_policy_path),
            summary=report.summary,
        )

    def _persist_runtime_summary(self, report: MassOfferQuarantineReport, *, report_path: Path, policy_path: Path) -> None:
        self.state.set_runtime_value("last_mass_offer_quarantine_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_quarantine_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_quarantine_window_days", report.window_days)
        self.state.set_runtime_value("last_mass_offer_quarantine_report_path", str(report_path))
        self.state.set_runtime_value("last_mass_offer_quarantine_policy_path", str(policy_path))
        self.state.set_runtime_value("last_mass_offer_quarantine_policy_entries", report.summary.get("policy_entries", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_clear_count", report.summary.get("clear_count", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_cooldown_count", report.summary.get("cooldown_count", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_dry_run_count", report.summary.get("dry_run_count", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_block_count", report.summary.get("block_count", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_active_count", report.summary.get("active_quarantine_count", 0))
        self.state.set_runtime_value("last_mass_offer_quarantine_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_quarantine_top_band", report.summary.get("top_band"))
        self.state.set_runtime_value("last_mass_offer_quarantine_top_expiry_at", report.summary.get("top_expiry_at"))
        self.state.set_runtime_value("last_mass_offer_quarantine_earliest_expiry_at", report.summary.get("earliest_expiry_at"))



def get_mass_offer_quarantine_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_quarantine_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_quarantine_chain"),
        "window_days": _coerce_int(runtime.get("last_mass_offer_quarantine_window_days")),
        "report_path": runtime.get("last_mass_offer_quarantine_report_path"),
        "policy_path": runtime.get("last_mass_offer_quarantine_policy_path"),
        "policy_entries": _coerce_int(runtime.get("last_mass_offer_quarantine_policy_entries")),
        "clear_count": _coerce_int(runtime.get("last_mass_offer_quarantine_clear_count")),
        "cooldown_count": _coerce_int(runtime.get("last_mass_offer_quarantine_cooldown_count")),
        "dry_run_count": _coerce_int(runtime.get("last_mass_offer_quarantine_dry_run_count")),
        "block_count": _coerce_int(runtime.get("last_mass_offer_quarantine_block_count")),
        "active_quarantine_count": _coerce_int(runtime.get("last_mass_offer_quarantine_active_count")),
        "top_collection": runtime.get("last_mass_offer_quarantine_top_collection"),
        "top_band": runtime.get("last_mass_offer_quarantine_top_band"),
        "top_expiry_at": runtime.get("last_mass_offer_quarantine_top_expiry_at"),
        "earliest_expiry_at": runtime.get("last_mass_offer_quarantine_earliest_expiry_at"),
    }



def format_mass_offer_quarantine_text(report: MassOfferQuarantineReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_quarantine",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"price_bnb={report.price_bnb:.6f}",
        f"window_days={report.window_days}",
        (
            f"active={report.summary.get('active_quarantine_count', 0)} "
            f"block={report.summary.get('block_count', 0)} "
            f"dry_run={report.summary.get('dry_run_count', 0)} "
            f"cooldown={report.summary.get('cooldown_count', 0)}"
        ),
    ]
    earliest_expiry = report.summary.get("earliest_expiry_at")
    if earliest_expiry:
        lines.append(f"earliest_expiry_at={earliest_expiry}")
    shown = 0
    for item in report.collections:
        if item.quarantine_band == "clear":
            continue
        lines.append(
            (
                f"- {item.display_name} [{item.quarantine_band}] | score={item.quarantine_score:.2f} | "
                f"until={item.expires_at or 'n/a'} | live={item.live_campaigns} | failed={item.failed_ratio:.2f} | "
                f"blocked={item.blocked_ratio:.2f}"
            )
        )
        if item.blocked_reason:
            lines.append(f"  reason={item.blocked_reason}")
        shown += 1
        if shown >= max(int(limit), 1):
            break
    if shown == 0:
        lines.append("- no_active_quarantine_entries")
    return "\n".join(lines)



def _build_quarantine_recommendation(
    *,
    settings: Settings,
    recommendation: CollectionBudgetRebalanceRecommendation,
    price_bnb: float,
    now: datetime,
) -> CollectionQuarantineRecommendation:
    score = 0.0
    notes: list[str] = [f"rebalance_band={recommendation.rebalance_band}"]
    rebalance_band = str(recommendation.rebalance_band or "").strip().lower() or None
    feedback_band = str(recommendation.feedback_band or "").strip().lower() or None
    allocation_band = str(recommendation.allocation_band or "").strip().lower() or None

    if rebalance_band == "stop":
        score -= 42.0
    elif rebalance_band == "cooldown":
        score -= 24.0
    elif rebalance_band == "trim":
        score -= 12.0

    if feedback_band == "pause":
        score -= 18.0
        notes.append("feedback_pause")
    elif feedback_band == "watch":
        score -= 10.0
    elif feedback_band == "throttle":
        score -= 6.0

    score -= min(max(float(recommendation.failed_ratio), 0.0) * 30.0, 25.0)
    score -= min(max(float(recommendation.blocked_ratio), 0.0) * 24.0, 18.0)
    score -= min(max(int(recommendation.no_submit_live_campaigns), 0) * 10.0, 25.0)

    if recommendation.realized_roi_pct is not None:
        if recommendation.realized_roi_pct <= -45.0:
            score -= 20.0
            notes.append(f"severe_realized_roi={recommendation.realized_roi_pct:.1f}%")
        elif recommendation.realized_roi_pct <= -20.0:
            score -= 10.0
            notes.append(f"negative_realized_roi={recommendation.realized_roi_pct:.1f}%")
        elif recommendation.realized_roi_pct >= 20.0:
            score += 6.0
    if recommendation.unrealized_roi_pct is not None and recommendation.current_active_offers > 0:
        if recommendation.unrealized_roi_pct <= -35.0:
            score -= 12.0
            notes.append(f"severe_unrealized_roi={recommendation.unrealized_roi_pct:.1f}%")
        elif recommendation.unrealized_roi_pct <= -15.0:
            score -= 6.0
    if recommendation.live_campaigns >= 2 and recommendation.submit_success_rate >= 0.75:
        score += 5.0
    if recommendation.blocked_reason:
        score -= 8.0
        notes.append(f"blocked_reason={recommendation.blocked_reason}")
    if recommendation.rebalance_budget_bnb > 0 and recommendation.current_active_exposure_bnb > recommendation.rebalance_budget_bnb + 1e-12:
        score -= 6.0
        notes.append("active_exposure_above_rebalance_budget")

    base_delay = max(
        float(recommendation.preferred_delay_seconds or recommendation.min_delay_seconds or settings.mass_offer_delay_seconds),
        0.5,
    )
    exposure_cap = max(float(recommendation.current_active_exposure_bnb or 0.0), float(price_bnb or 0.0)) or None

    band = "clear"
    expires_at: str | None = None
    enabled = True
    dry_run_only = False
    preferred_max_total: int | None = None
    max_total_cap: int | None = None
    preferred_delay_seconds: float | None = None
    min_delay_seconds: float | None = None
    max_active_offers: int | None = None
    max_active_exposure_bnb: float | None = None

    if rebalance_band == "stop" or score <= -50.0:
        band = "block"
        expires_at = (now + timedelta(hours=max(int(settings.mass_offer_quarantine_block_hours), 1))).isoformat()
        enabled = False
        notes.append("quarantine_block")
    elif rebalance_band == "cooldown" or score <= -30.0:
        band = "dry_run"
        expires_at = (now + timedelta(hours=max(int(settings.mass_offer_quarantine_dry_run_hours), 1))).isoformat()
        enabled = True
        dry_run_only = True
        preferred_max_total = 1
        max_total_cap = 1
        preferred_delay_seconds = max(base_delay * 3.0, base_delay + 2.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = exposure_cap
        notes.append("quarantine_dry_run")
    elif rebalance_band == "trim" or score <= -14.0:
        band = "cooldown"
        expires_at = (now + timedelta(hours=max(int(settings.mass_offer_quarantine_cooldown_hours), 1))).isoformat()
        enabled = True
        dry_run_only = False
        preferred_max_total = 1
        max_total_cap = 1
        preferred_delay_seconds = max(base_delay * 2.5, base_delay + 1.0)
        min_delay_seconds = preferred_delay_seconds
        max_active_offers = 1
        max_active_exposure_bnb = exposure_cap
        notes.append("quarantine_cooldown")

    return CollectionQuarantineRecommendation(
        collection_key=recommendation.collection_key,
        display_name=recommendation.display_name,
        chain=recommendation.chain,
        quarantine_band=band,
        quarantine_score=score,
        confidence=float(recommendation.confidence),
        enabled=enabled,
        dry_run_only=dry_run_only,
        expires_at=expires_at,
        preferred_max_total=preferred_max_total,
        max_total_cap=max_total_cap,
        preferred_delay_seconds=preferred_delay_seconds,
        min_delay_seconds=min_delay_seconds,
        max_active_offers=max_active_offers,
        max_active_exposure_bnb=max_active_exposure_bnb,
        rebalance_band=rebalance_band,
        feedback_band=feedback_band,
        allocation_band=allocation_band,
        live_campaigns=int(recommendation.live_campaigns),
        submitted_total=int(recommendation.submitted_total),
        target_total=int(recommendation.target_total),
        failed_ratio=float(recommendation.failed_ratio),
        blocked_ratio=float(recommendation.blocked_ratio),
        no_submit_live_campaigns=int(recommendation.no_submit_live_campaigns),
        current_active_offers=int(recommendation.current_active_offers),
        current_active_exposure_bnb=float(recommendation.current_active_exposure_bnb),
        rebalance_budget_bnb=float(recommendation.rebalance_budget_bnb),
        realized_roi_pct=recommendation.realized_roi_pct,
        unrealized_roi_pct=recommendation.unrealized_roi_pct,
        blocked_reason=recommendation.blocked_reason,
        notes=tuple(dict.fromkeys(notes)),
    )



def _hours_remaining(expires_at: str | None) -> float | None:
    if not expires_at:
        return None
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    return max((parsed - now).total_seconds() / 3600.0, 0.0)



def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")



def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
