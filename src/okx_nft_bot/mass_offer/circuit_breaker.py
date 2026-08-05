from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.tracker import MassOfferCampaign, MassOfferTracker
from okx_nft_bot.undercutter.state import PositionState


@dataclass(slots=True)
class MassOfferCircuitCollectionStatus:
    collection_key: str
    chain: str
    severity: str
    issue_code: str | None
    score: float
    live_campaigns: int
    target_total: int
    submitted_total: int
    failed_total: int
    blocked_total: int
    no_submit_live_campaigns: int
    target_utilization: float
    submit_success_rate: float
    failed_ratio: float
    blocked_ratio: float
    no_submit_ratio: float
    notes: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, float, str]:
        order = {"halt": 0, "caution": 1, "ok": 2}
        return (order.get(self.severity, 3), float(self.score), self.collection_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_key": self.collection_key,
            "chain": self.chain,
            "severity": self.severity,
            "issue_code": self.issue_code,
            "score": round(self.score, 3),
            "live_campaigns": self.live_campaigns,
            "target_total": self.target_total,
            "submitted_total": self.submitted_total,
            "failed_total": self.failed_total,
            "blocked_total": self.blocked_total,
            "no_submit_live_campaigns": self.no_submit_live_campaigns,
            "target_utilization": round(self.target_utilization, 4),
            "submit_success_rate": round(self.submit_success_rate, 4),
            "failed_ratio": round(self.failed_ratio, 4),
            "blocked_ratio": round(self.blocked_ratio, 4),
            "no_submit_ratio": round(self.no_submit_ratio, 4),
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class MassOfferCircuitReport:
    generated_at: str
    wallet: str | None
    chain: str
    window_hours: int
    report_path: str
    summary: dict[str, Any]
    collections: list[MassOfferCircuitCollectionStatus]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "window_hours": self.window_hours,
            "report_path": self.report_path,
            "summary": self.summary,
            "collections": [item.to_dict() for item in self.collections],
        }


class MassOfferCircuitBreaker:
    def __init__(
        self,
        *,
        settings: Settings,
        state: PositionState | None = None,
        tracker: MassOfferTracker | None = None,
    ) -> None:
        self.settings = settings
        self.state = state or PositionState(settings.execution_db_path)
        self.tracker = tracker or MassOfferTracker(settings.execution_db_path)

    def build_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_hours: int | None = None,
    ) -> MassOfferCircuitReport:
        resolved_chain = chain.strip().lower()
        resolved_window_hours = max(int(window_hours if window_hours is not None else self.settings.mass_offer_circuit_window_hours), 1)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=resolved_window_hours)
        campaigns = self.tracker.list_campaigns_since(chain=resolved_chain, since=since)
        submit_rows = {
            str(row.get("collection") or "").strip().lower(): row
            for row in self.state.get_collection_submit_stats(chain=resolved_chain, since=since)
        }
        campaign_rows = _aggregate_recent_campaigns(campaigns)

        collection_keys = sorted({*campaign_rows.keys(), *submit_rows.keys()})
        collections = [
            _build_collection_status(
                settings=self.settings,
                chain=resolved_chain,
                collection_key=collection_key,
                campaign=campaign_rows.get(collection_key, {}),
                submit=submit_rows.get(collection_key, {}),
            )
            for collection_key in collection_keys
        ]
        collections.sort(key=lambda item: item.sort_key())

        global_summary = _build_global_summary(
            settings=self.settings,
            collections=collections,
            recent_campaigns=campaigns,
            chain=resolved_chain,
        )
        report = MassOfferCircuitReport(
            generated_at=now.isoformat(),
            wallet=wallet or self.settings.buyer_wallet_address,
            chain=resolved_chain,
            window_hours=resolved_window_hours,
            report_path=str(self.settings.mass_offer_circuit_report_path),
            summary=global_summary,
            collections=collections,
        )
        return report

    def write_report(
        self,
        *,
        wallet: str | None = None,
        chain: str = "bsc",
        window_hours: int | None = None,
        report_path: Path | None = None,
    ) -> str:
        report = self.build_report(wallet=wallet, chain=chain, window_hours=window_hours)
        resolved_report_path = report_path or self.settings.mass_offer_circuit_report_path
        _write_json(resolved_report_path, report.to_dict())
        self._persist_runtime_summary(report, report_path=resolved_report_path)
        return str(resolved_report_path)

    def _persist_runtime_summary(self, report: MassOfferCircuitReport, *, report_path: Path) -> None:
        self.state.set_runtime_value("last_mass_offer_circuit_at", report.generated_at)
        self.state.set_runtime_value("last_mass_offer_circuit_chain", report.chain)
        self.state.set_runtime_value("last_mass_offer_circuit_window_hours", report.window_hours)
        self.state.set_runtime_value("last_mass_offer_circuit_report_path", str(report_path))
        self.state.set_runtime_value("last_mass_offer_circuit_collection_count", report.summary.get("collection_count", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_live_campaigns", report.summary.get("recent_live_campaigns", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_target_total", report.summary.get("recent_target_total", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_submitted_total", report.summary.get("recent_submitted_total", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_failed_total", report.summary.get("recent_failed_total", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_blocked_total", report.summary.get("recent_blocked_total", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_recent_no_submit_live_campaigns", report.summary.get("recent_no_submit_live_campaigns", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_target_utilization", report.summary.get("target_utilization", 0.0))
        self.state.set_runtime_value("last_mass_offer_circuit_submit_success_rate", report.summary.get("submit_success_rate", 0.0))
        self.state.set_runtime_value("last_mass_offer_circuit_failed_ratio", report.summary.get("failed_ratio", 0.0))
        self.state.set_runtime_value("last_mass_offer_circuit_blocked_ratio", report.summary.get("blocked_ratio", 0.0))
        self.state.set_runtime_value("last_mass_offer_circuit_no_submit_ratio", report.summary.get("no_submit_ratio", 0.0))
        self.state.set_runtime_value("last_mass_offer_circuit_severity", report.summary.get("severity"))
        self.state.set_runtime_value("last_mass_offer_circuit_issue_code", report.summary.get("issue_code"))
        self.state.set_runtime_value("last_mass_offer_circuit_should_block_live", "1" if report.summary.get("should_block_live") else "0")
        self.state.set_runtime_value("last_mass_offer_circuit_halt_count", report.summary.get("halt_count", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_caution_count", report.summary.get("caution_count", 0))
        self.state.set_runtime_value("last_mass_offer_circuit_top_collection", report.summary.get("top_collection"))
        self.state.set_runtime_value("last_mass_offer_circuit_top_issue_code", report.summary.get("top_issue_code"))


def get_mass_offer_circuit_runtime_summary(state: PositionState) -> dict[str, Any] | None:
    runtime = state.get_runtime_state()
    generated_at = runtime.get("last_mass_offer_circuit_at")
    if not generated_at:
        return None
    return {
        "generated_at": generated_at,
        "chain": runtime.get("last_mass_offer_circuit_chain"),
        "window_hours": _coerce_int(runtime.get("last_mass_offer_circuit_window_hours")),
        "report_path": runtime.get("last_mass_offer_circuit_report_path"),
        "collection_count": _coerce_int(runtime.get("last_mass_offer_circuit_collection_count")),
        "recent_live_campaigns": _coerce_int(runtime.get("last_mass_offer_circuit_recent_live_campaigns")),
        "recent_target_total": _coerce_int(runtime.get("last_mass_offer_circuit_recent_target_total")),
        "recent_submitted_total": _coerce_int(runtime.get("last_mass_offer_circuit_recent_submitted_total")),
        "recent_failed_total": _coerce_int(runtime.get("last_mass_offer_circuit_recent_failed_total")),
        "recent_blocked_total": _coerce_int(runtime.get("last_mass_offer_circuit_recent_blocked_total")),
        "recent_no_submit_live_campaigns": _coerce_int(runtime.get("last_mass_offer_circuit_recent_no_submit_live_campaigns")),
        "target_utilization": _coerce_float(runtime.get("last_mass_offer_circuit_target_utilization")),
        "submit_success_rate": _coerce_float(runtime.get("last_mass_offer_circuit_submit_success_rate")),
        "failed_ratio": _coerce_float(runtime.get("last_mass_offer_circuit_failed_ratio")),
        "blocked_ratio": _coerce_float(runtime.get("last_mass_offer_circuit_blocked_ratio")),
        "no_submit_ratio": _coerce_float(runtime.get("last_mass_offer_circuit_no_submit_ratio")),
        "severity": runtime.get("last_mass_offer_circuit_severity"),
        "issue_code": runtime.get("last_mass_offer_circuit_issue_code"),
        "should_block_live": bool(_coerce_optional_bool(runtime.get("last_mass_offer_circuit_should_block_live"))),
        "halt_count": _coerce_int(runtime.get("last_mass_offer_circuit_halt_count")),
        "caution_count": _coerce_int(runtime.get("last_mass_offer_circuit_caution_count")),
        "top_collection": runtime.get("last_mass_offer_circuit_top_collection"),
        "top_issue_code": runtime.get("last_mass_offer_circuit_top_issue_code"),
    }


def format_mass_offer_circuit_text(report: MassOfferCircuitReport, *, limit: int = 5) -> str:
    lines = [
        "mass_offer_circuit",
        f"wallet={report.wallet or 'not_configured'}",
        f"chain={report.chain}",
        f"window_hours={report.window_hours}",
        (
            f"severity={str(report.summary.get('severity') or 'ok').upper()} block_live={bool(report.summary.get('should_block_live'))} "
            f"issue={report.summary.get('issue_code') or 'none'}"
        ),
        (
            f"live_campaigns={report.summary.get('recent_live_campaigns', 0)} target={report.summary.get('recent_target_total', 0)} "
            f"submitted={report.summary.get('recent_submitted_total', 0)} failed={report.summary.get('recent_failed_total', 0)} "
            f"blocked={report.summary.get('recent_blocked_total', 0)}"
        ),
        (
            f"util={float(report.summary.get('target_utilization', 0.0)):.2f} success={float(report.summary.get('submit_success_rate', 0.0)):.2f} "
            f"failed_ratio={float(report.summary.get('failed_ratio', 0.0)):.2f} blocked_ratio={float(report.summary.get('blocked_ratio', 0.0)):.2f} "
            f"no_submit_ratio={float(report.summary.get('no_submit_ratio', 0.0)):.2f}"
        ),
    ]
    for item in report.collections[: max(int(limit), 1)]:
        lines.append(
            (
                f"- {item.collection_key} [{item.severity}] | issue={item.issue_code or 'none'} | score={item.score:.1f} | "
                f"live={item.live_campaigns} | util={item.target_utilization:.2f} | success={item.submit_success_rate:.2f} | "
                f"failed={item.failed_ratio:.2f} | blocked={item.blocked_ratio:.2f}"
            )
        )
    return "\n".join(lines)


def _aggregate_recent_campaigns(campaigns: list[MassOfferCampaign]) -> dict[str, dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for campaign in campaigns:
        if campaign.dry_run:
            continue
        key = campaign.collection.strip().lower()
        bucket = aggregated.setdefault(
            key,
            {
                "live_campaigns": 0,
                "target_total": 0,
                "submitted_total": 0,
                "failed_total": 0,
                "skipped_total": 0,
                "no_submit_live_campaigns": 0,
                "last_campaign_at": None,
            },
        )
        bucket["live_campaigns"] += 1
        bucket["target_total"] += int(campaign.target_count)
        bucket["submitted_total"] += int(campaign.submitted_count)
        bucket["failed_total"] += int(campaign.failed_count)
        bucket["skipped_total"] += int(campaign.skipped_count)
        if int(campaign.submitted_count) <= 0:
            bucket["no_submit_live_campaigns"] += 1
        last_campaign_at = bucket.get("last_campaign_at")
        if last_campaign_at is None or campaign.updated_at.isoformat() > str(last_campaign_at):
            bucket["last_campaign_at"] = campaign.updated_at.isoformat()
    return aggregated


def _build_collection_status(
    *,
    settings: Settings,
    chain: str,
    collection_key: str,
    campaign: dict[str, Any],
    submit: dict[str, Any],
) -> MassOfferCircuitCollectionStatus:
    live_campaigns = int(campaign.get("live_campaigns", 0) or 0)
    target_total = int(campaign.get("target_total", 0) or 0)
    submitted_total = int(campaign.get("submitted_total", 0) or 0)
    failed_total = max(
        int(campaign.get("failed_total", 0) or 0),
        int(submit.get("failed_count", 0) or 0),
    )
    blocked_total = int(submit.get("blocked_count", 0) or 0)
    no_submit_live_campaigns = int(campaign.get("no_submit_live_campaigns", 0) or 0)

    submit_attempts = submitted_total + failed_total + blocked_total
    target_utilization = submitted_total / target_total if target_total > 0 else 0.0
    submit_success_rate = submitted_total / submit_attempts if submit_attempts > 0 else 0.0
    failed_ratio = failed_total / max(target_total, 1) if target_total > 0 else 0.0
    blocked_ratio = blocked_total / submit_attempts if submit_attempts > 0 else 0.0
    no_submit_ratio = no_submit_live_campaigns / live_campaigns if live_campaigns > 0 else 0.0

    min_live = max(int(settings.mass_offer_circuit_min_live_campaigns), 1)
    min_target = max(int(settings.mass_offer_circuit_min_target_total), 1)
    failed_limit = float(settings.mass_offer_circuit_max_failed_ratio)
    blocked_limit = float(settings.mass_offer_circuit_max_blocked_ratio)
    no_submit_limit = float(settings.mass_offer_circuit_max_no_submit_ratio)
    caution_factor = _clamp(float(settings.mass_offer_circuit_caution_factor), 0.1, 0.95)

    notes: list[str] = []
    issues: list[tuple[int, str]] = []
    score = 0.0

    has_signal = live_campaigns >= min_live or target_total >= min_target or submit_attempts > 0
    if not has_signal:
        notes.append("insufficient_recent_live_signal")
    else:
        if live_campaigns >= min_live and submitted_total <= 0:
            score -= 60.0
            notes.append("zero_live_submits")
            issues.append((100, "zero_live_submits"))
        if target_total >= min_target:
            if target_utilization < 0.20:
                score -= 28.0
                notes.append(f"weak_target_utilization={target_utilization:.2f}")
                issues.append((70, "weak_target_utilization"))
            elif target_utilization < 0.40:
                score -= 12.0
                notes.append(f"soft_target_utilization={target_utilization:.2f}")
            elif target_utilization >= 0.75:
                score += 8.0
                notes.append(f"healthy_target_utilization={target_utilization:.2f}")
        if failed_ratio >= failed_limit and target_total >= min_target:
            score -= 42.0
            notes.append(f"failed_ratio={failed_ratio:.2f}")
            issues.append((90, "high_failed_ratio"))
        elif failed_ratio >= failed_limit * caution_factor and target_total >= min_target:
            score -= 18.0
            notes.append(f"elevated_failed_ratio={failed_ratio:.2f}")
            issues.append((45, "elevated_failed_ratio"))
        if blocked_ratio >= blocked_limit and submit_attempts > 0:
            score -= 40.0
            notes.append(f"blocked_ratio={blocked_ratio:.2f}")
            issues.append((85, "high_blocked_ratio"))
        elif blocked_ratio >= blocked_limit * caution_factor and submit_attempts > 0:
            score -= 16.0
            notes.append(f"elevated_blocked_ratio={blocked_ratio:.2f}")
            issues.append((40, "elevated_blocked_ratio"))
        if no_submit_ratio >= no_submit_limit and live_campaigns >= min_live:
            score -= 36.0
            notes.append(f"no_submit_ratio={no_submit_ratio:.2f}")
            issues.append((80, "high_no_submit_ratio"))
        elif no_submit_ratio >= no_submit_limit * caution_factor and live_campaigns >= min_live:
            score -= 14.0
            notes.append(f"elevated_no_submit_ratio={no_submit_ratio:.2f}")
            issues.append((35, "elevated_no_submit_ratio"))
        if submit_attempts > 0 and submit_success_rate >= 0.70:
            score += 6.0
            notes.append(f"healthy_submit_success={submit_success_rate:.2f}")
        elif submit_attempts > 0 and submit_success_rate < 0.40:
            score -= 12.0
            notes.append(f"weak_submit_success={submit_success_rate:.2f}")

    score = _clamp(score, -100.0, 30.0)
    issue_code = max(issues, key=lambda item: item[0])[1] if issues else None
    hard_issue = any(priority >= 80 for priority, _ in issues)
    soft_issue = bool(issues)
    if hard_issue or score <= -40.0:
        severity = "halt"
    elif soft_issue or score <= -15.0:
        severity = "caution"
    else:
        severity = "ok"
    return MassOfferCircuitCollectionStatus(
        collection_key=collection_key,
        chain=chain,
        severity=severity,
        issue_code=issue_code,
        score=score,
        live_campaigns=live_campaigns,
        target_total=target_total,
        submitted_total=submitted_total,
        failed_total=failed_total,
        blocked_total=blocked_total,
        no_submit_live_campaigns=no_submit_live_campaigns,
        target_utilization=target_utilization,
        submit_success_rate=submit_success_rate,
        failed_ratio=failed_ratio,
        blocked_ratio=blocked_ratio,
        no_submit_ratio=no_submit_ratio,
        notes=tuple(dict.fromkeys(note for note in notes if note)),
    )


def _build_global_summary(
    *,
    settings: Settings,
    collections: list[MassOfferCircuitCollectionStatus],
    recent_campaigns: list[MassOfferCampaign],
    chain: str,
) -> dict[str, Any]:
    live_campaigns = sum(item.live_campaigns for item in collections)
    recent_target_total = sum(item.target_total for item in collections)
    recent_submitted_total = sum(item.submitted_total for item in collections)
    recent_failed_total = sum(item.failed_total for item in collections)
    recent_blocked_total = sum(item.blocked_total for item in collections)
    recent_no_submit_live_campaigns = sum(item.no_submit_live_campaigns for item in collections)
    submit_attempts = recent_submitted_total + recent_failed_total + recent_blocked_total
    target_utilization = recent_submitted_total / recent_target_total if recent_target_total > 0 else 0.0
    submit_success_rate = recent_submitted_total / submit_attempts if submit_attempts > 0 else 0.0
    failed_ratio = recent_failed_total / max(recent_target_total, 1) if recent_target_total > 0 else 0.0
    blocked_ratio = recent_blocked_total / submit_attempts if submit_attempts > 0 else 0.0
    no_submit_ratio = recent_no_submit_live_campaigns / live_campaigns if live_campaigns > 0 else 0.0
    halt_count = sum(1 for item in collections if item.severity == "halt")
    caution_count = sum(1 for item in collections if item.severity == "caution")
    top_collection = collections[0].collection_key if collections else None
    top_issue_code = collections[0].issue_code if collections else None

    failed_limit = float(settings.mass_offer_circuit_max_failed_ratio)
    blocked_limit = float(settings.mass_offer_circuit_max_blocked_ratio)
    no_submit_limit = float(settings.mass_offer_circuit_max_no_submit_ratio)
    caution_factor = _clamp(float(settings.mass_offer_circuit_caution_factor), 0.1, 0.95)
    min_live = max(int(settings.mass_offer_circuit_min_live_campaigns), 1)
    min_target = max(int(settings.mass_offer_circuit_min_target_total), 1)

    severity = "ok"
    issue_code: str | None = None
    if halt_count > 0:
        severity = "halt"
        issue_code = top_issue_code or "collection_halt"
    elif live_campaigns >= min_live and recent_submitted_total <= 0:
        severity = "halt"
        issue_code = "zero_live_submits"
    elif recent_target_total >= min_target and failed_ratio >= failed_limit:
        severity = "halt"
        issue_code = "high_failed_ratio"
    elif submit_attempts > 0 and blocked_ratio >= blocked_limit:
        severity = "halt"
        issue_code = "high_blocked_ratio"
    elif live_campaigns >= min_live and no_submit_ratio >= no_submit_limit:
        severity = "halt"
        issue_code = "high_no_submit_ratio"
    elif caution_count > 0:
        severity = "caution"
        issue_code = top_issue_code or "collection_caution"
    elif recent_target_total >= min_target and failed_ratio >= failed_limit * caution_factor:
        severity = "caution"
        issue_code = "elevated_failed_ratio"
    elif submit_attempts > 0 and blocked_ratio >= blocked_limit * caution_factor:
        severity = "caution"
        issue_code = "elevated_blocked_ratio"
    elif live_campaigns >= min_live and no_submit_ratio >= no_submit_limit * caution_factor:
        severity = "caution"
        issue_code = "elevated_no_submit_ratio"

    return {
        "chain": chain,
        "collection_count": len(collections),
        "recent_live_campaigns": live_campaigns,
        "recent_campaign_count": len([item for item in recent_campaigns if not item.dry_run]),
        "recent_target_total": recent_target_total,
        "recent_submitted_total": recent_submitted_total,
        "recent_failed_total": recent_failed_total,
        "recent_blocked_total": recent_blocked_total,
        "recent_no_submit_live_campaigns": recent_no_submit_live_campaigns,
        "target_utilization": round(target_utilization, 4),
        "submit_success_rate": round(submit_success_rate, 4),
        "failed_ratio": round(failed_ratio, 4),
        "blocked_ratio": round(blocked_ratio, 4),
        "no_submit_ratio": round(no_submit_ratio, 4),
        "halt_count": halt_count,
        "caution_count": caution_count,
        "severity": severity,
        "issue_code": issue_code,
        "should_block_live": bool(settings.mass_offer_circuit_blocks_live and severity == "halt"),
        "top_collection": top_collection,
        "top_issue_code": top_issue_code,
    }


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
