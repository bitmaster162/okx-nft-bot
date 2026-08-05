from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.undercutter.state import PositionState


_SEVERITY_RANK = {
    "OK": 0,
    "CAUTION": 1,
    "HIGH_RISK": 2,
    "BLOCK": 3,
}


@dataclass(slots=True)
class ExecutionHealthIssue:
    code: str
    severity: str
    message: str
    value: float | int | str | None = None
    threshold: float | int | str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "context": dict(self.context),
        }


@dataclass(slots=True)
class ExecutionHealthSummary:
    chain: str
    window_hours: int
    event_limit: int
    burst_window_minutes: int
    severity: str
    block_live_submits: bool
    auto_force_dry_run_applied: bool
    issue_count: int
    total_events: int
    attempt_count: int
    submitted_count: int
    failed_count: int
    blocked_count: int
    cancelled_count: int
    failure_ratio: float
    blocked_ratio: float
    consecutive_failures: int
    recent_failure_burst: int
    latest_event_at: str | None
    top_issue_code: str | None
    top_issue_message: str | None
    top_failing_engine: str | None = None
    top_failing_engine_count: int = 0
    top_failing_collection: str | None = None
    top_failing_collection_count: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "window_hours": self.window_hours,
            "event_limit": self.event_limit,
            "burst_window_minutes": self.burst_window_minutes,
            "severity": self.severity,
            "block_live_submits": self.block_live_submits,
            "auto_force_dry_run_applied": self.auto_force_dry_run_applied,
            "issue_count": self.issue_count,
            "total_events": self.total_events,
            "attempt_count": self.attempt_count,
            "submitted_count": self.submitted_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "cancelled_count": self.cancelled_count,
            "failure_ratio": round(self.failure_ratio, 6),
            "blocked_ratio": round(self.blocked_ratio, 6),
            "consecutive_failures": self.consecutive_failures,
            "recent_failure_burst": self.recent_failure_burst,
            "latest_event_at": self.latest_event_at,
            "top_issue_code": self.top_issue_code,
            "top_issue_message": self.top_issue_message,
            "top_failing_engine": self.top_failing_engine,
            "top_failing_engine_count": self.top_failing_engine_count,
            "top_failing_collection": self.top_failing_collection,
            "top_failing_collection_count": self.top_failing_collection_count,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class ExecutionHealthReport:
    generated_at: str
    chain: str
    summary: ExecutionHealthSummary
    issues: list[ExecutionHealthIssue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "chain": self.chain,
            "summary": self.summary.to_dict(),
            "issues": [item.to_dict() for item in self.issues],
        }


class ExecutionHealthAnalyzer:
    def __init__(
        self,
        *,
        settings: Settings,
        state: PositionState | None = None,
    ) -> None:
        self.settings = settings
        self.state = state or PositionState(settings.execution_db_path)

    def build_report(
        self,
        *,
        chain: str | None = None,
        window_hours: int | None = None,
        event_limit: int | None = None,
    ) -> ExecutionHealthReport:
        resolved_chain = (chain or self.settings.execution_chain).strip().lower()
        resolved_window_hours = max(
            int(window_hours if window_hours is not None else self.settings.execution_health_window_hours),
            1,
        )
        resolved_event_limit = max(
            int(event_limit if event_limit is not None else self.settings.execution_health_event_limit),
            1,
        )
        resolved_burst_window_minutes = max(int(self.settings.execution_health_failure_burst_window_minutes), 1)
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=resolved_window_hours)
        burst_start = now - timedelta(minutes=resolved_burst_window_minutes)
        events = self.state.list_submit_events(
            chain=resolved_chain,
            since=window_start,
            limit=resolved_event_limit,
        )

        total_events = len(events)
        submitted_count = 0
        failed_count = 0
        blocked_count = 0
        cancelled_count = 0
        failed_by_engine: dict[str, int] = {}
        failed_by_collection: dict[str, int] = {}
        latest_event_at = events[0]["created_at"] if events else None
        recent_failure_burst = 0

        for event in events:
            status = _normalized_status(event.get("status"))
            created_at = _parse_dt(event.get("created_at"))
            if status == "submitted":
                submitted_count += 1
            elif status == "failed":
                failed_count += 1
                if created_at is not None and created_at >= burst_start:
                    recent_failure_burst += 1
                engine = str(event.get("engine") or "unknown").strip().lower() or "unknown"
                collection = str(event.get("collection") or "unknown").strip().lower() or "unknown"
                failed_by_engine[engine] = failed_by_engine.get(engine, 0) + 1
                failed_by_collection[collection] = failed_by_collection.get(collection, 0) + 1
            elif status == "blocked":
                blocked_count += 1
            elif status == "cancelled":
                cancelled_count += 1

        attempt_count = submitted_count + failed_count
        failure_ratio = failed_count / max(attempt_count, 1) if attempt_count > 0 else 0.0
        blocked_ratio = blocked_count / max(total_events, 1) if total_events > 0 else 0.0
        consecutive_failures = _consecutive_failures(events)
        top_failing_engine, top_failing_engine_count = _top_entry(failed_by_engine)
        top_failing_collection, top_failing_collection_count = _top_entry(failed_by_collection)

        notes: list[str] = []
        issues: list[ExecutionHealthIssue] = []
        min_attempts = max(int(self.settings.execution_health_min_attempts), 1)
        max_failure_ratio = max(float(self.settings.execution_health_max_failure_ratio), 0.0)
        max_consecutive_failures = max(int(self.settings.execution_health_max_consecutive_failures), 0)
        max_failure_burst = max(int(self.settings.execution_health_max_failure_burst), 0)
        caution_factor = min(max(float(self.settings.execution_health_caution_factor), 0.0), 1.0) or 0.6

        if total_events == 0:
            notes.append("no_events_in_window")
        if total_events >= resolved_event_limit:
            notes.append("event_limit_reached")
        if attempt_count < min_attempts:
            notes.append("insufficient_attempts_for_failure_ratio")

        if attempt_count >= min_attempts:
            if submitted_count == 0 and failed_count >= min_attempts:
                issues.append(
                    ExecutionHealthIssue(
                        code="no_successful_submits",
                        severity="BLOCK",
                        message=(
                            f"Recent execution window contains {failed_count} failed submit(s) and no successful live submits."
                        ),
                        value=failed_count,
                        threshold=min_attempts,
                        context={"window_hours": resolved_window_hours, "chain": resolved_chain},
                    )
                )
            elif failure_ratio >= max_failure_ratio - 1e-12:
                issues.append(
                    ExecutionHealthIssue(
                        code="high_failure_ratio",
                        severity="BLOCK",
                        message=(
                            f"Recent execution failure ratio is {failure_ratio:.2f}, above the configured limit of {max_failure_ratio:.2f}."
                        ),
                        value=round(failure_ratio, 6),
                        threshold=round(max_failure_ratio, 6),
                        context={"attempt_count": attempt_count, "failed_count": failed_count},
                    )
                )
            elif failure_ratio >= (max_failure_ratio * caution_factor) - 1e-12:
                issues.append(
                    ExecutionHealthIssue(
                        code="elevated_failure_ratio",
                        severity="CAUTION",
                        message=(
                            f"Recent execution failure ratio is {failure_ratio:.2f}, approaching the configured limit of {max_failure_ratio:.2f}."
                        ),
                        value=round(failure_ratio, 6),
                        threshold=round(max_failure_ratio * caution_factor, 6),
                        context={"attempt_count": attempt_count, "failed_count": failed_count},
                    )
                )

        if max_consecutive_failures > 0:
            caution_consecutive = max(2, _ceil_threshold(max_consecutive_failures * caution_factor))
            if consecutive_failures >= max_consecutive_failures:
                issues.append(
                    ExecutionHealthIssue(
                        code="consecutive_failures",
                        severity="BLOCK",
                        message=(
                            f"Recent execution history contains {consecutive_failures} consecutive failed submits."
                        ),
                        value=consecutive_failures,
                        threshold=max_consecutive_failures,
                        context={"chain": resolved_chain},
                    )
                )
            elif consecutive_failures >= caution_consecutive:
                issues.append(
                    ExecutionHealthIssue(
                        code="elevated_consecutive_failures",
                        severity="HIGH_RISK",
                        message=(
                            f"Recent execution history contains {consecutive_failures} consecutive failed submits."
                        ),
                        value=consecutive_failures,
                        threshold=caution_consecutive,
                        context={"chain": resolved_chain},
                    )
                )

        if max_failure_burst > 0:
            caution_burst = max(2, _ceil_threshold(max_failure_burst * caution_factor))
            if recent_failure_burst >= max_failure_burst:
                issues.append(
                    ExecutionHealthIssue(
                        code="failure_burst",
                        severity="BLOCK",
                        message=(
                            f"Recent execution window contains {recent_failure_burst} failed submits in the last {resolved_burst_window_minutes} minute(s)."
                        ),
                        value=recent_failure_burst,
                        threshold=max_failure_burst,
                        context={"burst_window_minutes": resolved_burst_window_minutes},
                    )
                )
            elif recent_failure_burst >= caution_burst:
                issues.append(
                    ExecutionHealthIssue(
                        code="elevated_failure_burst",
                        severity="HIGH_RISK",
                        message=(
                            f"Recent execution window contains {recent_failure_burst} failed submits in the last {resolved_burst_window_minutes} minute(s)."
                        ),
                        value=recent_failure_burst,
                        threshold=caution_burst,
                        context={"burst_window_minutes": resolved_burst_window_minutes},
                    )
                )

        if blocked_count >= min_attempts and blocked_ratio >= 0.5 - 1e-12:
            issues.append(
                ExecutionHealthIssue(
                    code="blocked_submission_pressure",
                    severity="CAUTION",
                    message=(
                        f"Recent execution window contains {blocked_count} blocked submit attempt(s); check live-arm, dry-run, and guardrail state."
                    ),
                    value=blocked_count,
                    threshold=min_attempts,
                    context={"blocked_ratio": round(blocked_ratio, 6)},
                )
            )

        worst = _worst_issue(issues)
        summary = ExecutionHealthSummary(
            chain=resolved_chain,
            window_hours=resolved_window_hours,
            event_limit=resolved_event_limit,
            burst_window_minutes=resolved_burst_window_minutes,
            severity=worst.severity if worst else "OK",
            block_live_submits=any(issue.severity == "BLOCK" for issue in issues),
            auto_force_dry_run_applied=False,
            issue_count=len(issues),
            total_events=total_events,
            attempt_count=attempt_count,
            submitted_count=submitted_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            cancelled_count=cancelled_count,
            failure_ratio=failure_ratio,
            blocked_ratio=blocked_ratio,
            consecutive_failures=consecutive_failures,
            recent_failure_burst=recent_failure_burst,
            latest_event_at=latest_event_at,
            top_issue_code=worst.code if worst else None,
            top_issue_message=worst.message if worst else None,
            top_failing_engine=top_failing_engine,
            top_failing_engine_count=top_failing_engine_count,
            top_failing_collection=top_failing_collection,
            top_failing_collection_count=top_failing_collection_count,
            notes=tuple(dict.fromkeys(notes)),
        )
        return ExecutionHealthReport(
            generated_at=now.isoformat(),
            chain=resolved_chain,
            summary=summary,
            issues=_sorted_issues(issues),
        )

    def evaluate_and_apply(
        self,
        *,
        chain: str | None = None,
        window_hours: int | None = None,
        event_limit: int | None = None,
    ) -> ExecutionHealthReport:
        report = self.build_report(
            chain=chain,
            window_hours=window_hours,
            event_limit=event_limit,
        )
        applied = False
        if (
            self.settings.execution_health_enabled
            and self.settings.execution_health_auto_force_dry_run
            and report.summary.block_live_submits
        ):
            reason = f"execution_health:{report.summary.top_issue_code or 'block'}"
            self.state.set_force_dry_run(True, reason=reason)
            applied = True
        report.summary.auto_force_dry_run_applied = applied
        self._persist_runtime_summary(report)
        return report

    def write_report(
        self,
        *,
        chain: str | None = None,
        window_hours: int | None = None,
        event_limit: int | None = None,
        report_path: Path | None = None,
        apply_guardrails: bool = False,
    ) -> str:
        report = (
            self.evaluate_and_apply(
                chain=chain,
                window_hours=window_hours,
                event_limit=event_limit,
            )
            if apply_guardrails
            else self.build_report(
                chain=chain,
                window_hours=window_hours,
                event_limit=event_limit,
            )
        )
        target = report_path or self.settings.execution_health_report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)

    def _persist_runtime_summary(self, report: ExecutionHealthReport) -> None:
        self.state.set_runtime_value("last_execution_health_at", report.generated_at)
        self.state.set_runtime_value("last_execution_health_severity", report.summary.severity)
        self.state.set_runtime_value("last_execution_health_block", "1" if report.summary.block_live_submits else "0")
        self.state.set_runtime_value("last_execution_health_reason", report.summary.top_issue_code)
        self.state.set_runtime_value("last_execution_health_reason_text", report.summary.top_issue_message)
        self.state.set_runtime_value("last_execution_health_failed_count", report.summary.failed_count)
        self.state.set_runtime_value("last_execution_health_submitted_count", report.summary.submitted_count)
        self.state.set_runtime_value("last_execution_health_blocked_count", report.summary.blocked_count)
        self.state.set_runtime_value("last_execution_health_failure_ratio", f"{report.summary.failure_ratio:.6f}")
        self.state.set_runtime_value("last_execution_health_consecutive_failures", report.summary.consecutive_failures)
        self.state.set_runtime_value("last_execution_health_failure_burst", report.summary.recent_failure_burst)



def format_execution_health_text(report: ExecutionHealthReport, *, limit: int = 5) -> str:
    summary = report.summary
    lines = ["execution_health"]
    lines.append(f"chain={summary.chain} window_hours={summary.window_hours} burst_window_minutes={summary.burst_window_minutes}")
    lines.append(f"severity={summary.severity}")
    lines.append(
        f"block_live_submits={str(summary.block_live_submits).lower()} auto_force_dry_run_applied={str(summary.auto_force_dry_run_applied).lower()}"
    )
    lines.append(
        f"events={summary.total_events} attempts={summary.attempt_count} submitted={summary.submitted_count} failed={summary.failed_count} blocked={summary.blocked_count} cancelled={summary.cancelled_count}"
    )
    lines.append(
        f"failure_ratio={summary.failure_ratio:.4f} blocked_ratio={summary.blocked_ratio:.4f} consecutive_failures={summary.consecutive_failures} failure_burst={summary.recent_failure_burst}"
    )
    if summary.top_failing_engine:
        lines.append(f"top_failing_engine={summary.top_failing_engine} count={summary.top_failing_engine_count}")
    if summary.top_failing_collection:
        lines.append(f"top_failing_collection={summary.top_failing_collection} count={summary.top_failing_collection_count}")
    if summary.latest_event_at:
        lines.append(f"latest_event_at={summary.latest_event_at}")
    if summary.top_issue_code:
        lines.append(f"top_issue={summary.top_issue_code}")
    if summary.top_issue_message:
        lines.append(f"top_message={summary.top_issue_message}")
    if summary.notes:
        lines.append(f"notes={','.join(summary.notes)}")
    if report.issues:
        lines.append("issues:")
        for issue in report.issues[: max(int(limit), 0)]:
            extra = []
            if issue.threshold is not None:
                extra.append(f"threshold={issue.threshold}")
            if issue.value is not None:
                extra.append(f"value={issue.value}")
            suffix = f" [{' | '.join(extra)}]" if extra else ""
            lines.append(f"- {issue.severity} {issue.code}: {issue.message}{suffix}")
    return "\n".join(lines)



def _normalized_status(raw_value: Any) -> str:
    return str(raw_value or "").strip().lower()



def _parse_dt(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed



def _consecutive_failures(events: list[dict[str, Any]]) -> int:
    streak = 0
    for event in events:
        status = _normalized_status(event.get("status"))
        if status == "failed":
            streak += 1
            continue
        if status == "submitted":
            break
    return streak



def _ceil_threshold(value: float) -> int:
    integer = int(value)
    return integer if float(integer) == float(value) else integer + 1



def _top_entry(values: dict[str, int]) -> tuple[str | None, int]:
    if not values:
        return None, 0
    key, count = sorted(values.items(), key=lambda item: (-item[1], item[0]))[0]
    return key, count



def _worst_issue(issues: list[ExecutionHealthIssue]) -> ExecutionHealthIssue | None:
    if not issues:
        return None
    return sorted(
        issues,
        key=lambda item: (-_SEVERITY_RANK.get(item.severity, -1), item.code),
    )[0]



def _sorted_issues(issues: list[ExecutionHealthIssue]) -> list[ExecutionHealthIssue]:
    return sorted(
        issues,
        key=lambda item: (-_SEVERITY_RANK.get(item.severity, -1), item.code),
    )
