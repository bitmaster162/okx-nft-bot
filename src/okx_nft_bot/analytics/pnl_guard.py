from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from okx_nft_bot.analytics.portfolio import WalletPnlAnalyzer
from okx_nft_bot.config import Settings
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_SEVERITY_RANK = {
    "OK": 0,
    "CAUTION": 1,
    "HIGH_RISK": 2,
    "BLOCK": 3,
}

_NATIVE_CURRENCY_BY_CHAIN = {
    "bsc": "BNB",
    "bnb": "BNB",
    "eth": "ETH",
    "ethereum": "ETH",
    "matic": "MATIC",
    "polygon": "MATIC",
    "sol": "SOL",
    "solana": "SOL",
}


@dataclass(slots=True)
class PnlGuardBreach:
    code: str
    severity: str
    message: str
    value: float | int | str | None = None
    threshold: float | int | str | None = None
    currency: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "currency": self.currency,
            "context": dict(self.context),
        }


@dataclass(slots=True)
class PnlGuardSummary:
    wallet: str | None
    chain: str
    currency: str
    window_hours: int
    severity: str
    block_live_submits: bool
    auto_force_dry_run_applied: bool
    breach_count: int
    closed_position_count: int
    winning_closed_count: int
    losing_closed_count: int
    breakeven_closed_count: int
    recent_loss_streak: int
    realized_pnl_native: float | None
    latest_exit_time: str | None
    top_breach_code: str | None
    top_breach_message: str | None
    caution_loss_threshold: float | None = None
    loss_limit: float | None = None
    profit_lock_threshold: float | None = None
    top_winning_collection: str | None = None
    top_winning_pnl: float | None = None
    top_losing_collection: str | None = None
    top_losing_pnl: float | None = None
    realized_pnl_by_currency: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "chain": self.chain,
            "currency": self.currency,
            "window_hours": self.window_hours,
            "severity": self.severity,
            "block_live_submits": self.block_live_submits,
            "auto_force_dry_run_applied": self.auto_force_dry_run_applied,
            "breach_count": self.breach_count,
            "closed_position_count": self.closed_position_count,
            "winning_closed_count": self.winning_closed_count,
            "losing_closed_count": self.losing_closed_count,
            "breakeven_closed_count": self.breakeven_closed_count,
            "recent_loss_streak": self.recent_loss_streak,
            "realized_pnl_native": round(self.realized_pnl_native, 6) if self.realized_pnl_native is not None else None,
            "latest_exit_time": self.latest_exit_time,
            "top_breach_code": self.top_breach_code,
            "top_breach_message": self.top_breach_message,
            "caution_loss_threshold": round(self.caution_loss_threshold, 6) if self.caution_loss_threshold is not None else None,
            "loss_limit": round(self.loss_limit, 6) if self.loss_limit is not None else None,
            "profit_lock_threshold": round(self.profit_lock_threshold, 6) if self.profit_lock_threshold is not None else None,
            "top_winning_collection": self.top_winning_collection,
            "top_winning_pnl": round(self.top_winning_pnl, 6) if self.top_winning_pnl is not None else None,
            "top_losing_collection": self.top_losing_collection,
            "top_losing_pnl": round(self.top_losing_pnl, 6) if self.top_losing_pnl is not None else None,
            "realized_pnl_by_currency": {key: round(value, 6) for key, value in sorted(self.realized_pnl_by_currency.items())},
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class PnlGuardReport:
    generated_at: str
    wallet: str | None
    chain: str
    currency: str
    summary: PnlGuardSummary
    breaches: list[PnlGuardBreach]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "currency": self.currency,
            "summary": self.summary.to_dict(),
            "breaches": [item.to_dict() for item in self.breaches],
        }


class PnlGuardAnalyzer:
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

    def build_report(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
    ) -> PnlGuardReport:
        resolved_chain = (chain or self.settings.execution_chain).strip().lower()
        resolved_wallet = (wallet or self.settings.buyer_wallet_address or "").strip().lower() or None
        resolved_window_hours = max(int(window_hours if window_hours is not None else self.settings.pnl_guard_window_hours), 1)
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=resolved_window_hours)
        native_currency = _native_currency_for_chain(resolved_chain)
        pnl_report = WalletPnlAnalyzer(settings=self.settings, store=self.store).build_report(
            wallet=resolved_wallet,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            collection_limit=10,
            open_limit=10,
            closed_limit=None,
        )

        breaches: list[PnlGuardBreach] = []
        notes: list[str] = list(pnl_report.summary.notes)
        realized_by_currency: dict[str, float] = {}
        realized_by_collection: dict[str, float] = {}
        native_closed = []
        latest_exit_time: str | None = None

        for item in pnl_report.closed_positions:
            exit_dt = _parse_dt(item.exit_time)
            if exit_dt is None or exit_dt < window_start:
                continue
            latest_exit_time = item.exit_time if latest_exit_time is None or item.exit_time > latest_exit_time else latest_exit_time
            realized_by_currency[item.currency] = realized_by_currency.get(item.currency, 0.0) + float(item.realized_pnl)
            if item.currency == native_currency:
                native_closed.append(item)
                realized_by_collection[item.collection] = realized_by_collection.get(item.collection, 0.0) + float(item.realized_pnl)

        native_realized = realized_by_currency.get(native_currency)
        loss_limit = float(self.settings.pnl_guard_max_daily_loss_bnb) if self.settings.pnl_guard_max_daily_loss_bnb > 0 else None
        profit_lock = float(self.settings.pnl_guard_profit_lock_bnb) if self.settings.pnl_guard_profit_lock_bnb > 0 else None
        caution_threshold = (-loss_limit * 0.5) if loss_limit is not None else None
        winning_closed_count = sum(1 for item in native_closed if float(item.realized_pnl) > 1e-12)
        losing_closed_count = sum(1 for item in native_closed if float(item.realized_pnl) < -1e-12)
        breakeven_closed_count = max(len(native_closed) - winning_closed_count - losing_closed_count, 0)
        recent_loss_streak = _recent_loss_streak(native_closed)

        if not resolved_wallet:
            breaches.append(
                PnlGuardBreach(
                    code="wallet_not_configured",
                    severity="CAUTION",
                    message="Buyer wallet is not configured; session PnL guard is running blind.",
                )
            )
        if not native_closed:
            notes.append("no_closed_positions_in_window")
        if native_realized is None:
            notes.append(f"no_{native_currency.lower()}_closed_positions_in_window")

        if loss_limit is not None and native_realized is not None:
            if native_realized <= -loss_limit - 1e-12:
                breaches.append(
                    PnlGuardBreach(
                        code="daily_loss_limit",
                        severity="BLOCK",
                        message=(
                            f"Session realized PnL is {native_realized:.6f} {native_currency}, below the configured loss limit of {-loss_limit:.6f} {native_currency}."
                        ),
                        value=round(native_realized, 6),
                        threshold=round(-loss_limit, 6),
                        currency=native_currency,
                        context={"window_hours": resolved_window_hours},
                    )
                )
            elif caution_threshold is not None and native_realized <= caution_threshold - 1e-12:
                breaches.append(
                    PnlGuardBreach(
                        code="daily_loss_caution",
                        severity="CAUTION",
                        message=(
                            f"Session realized PnL is {native_realized:.6f} {native_currency}, approaching the configured loss limit of {-loss_limit:.6f} {native_currency}."
                        ),
                        value=round(native_realized, 6),
                        threshold=round(caution_threshold, 6),
                        currency=native_currency,
                        context={"window_hours": resolved_window_hours},
                    )
                )

        if (
            profit_lock is not None
            and native_realized is not None
            and native_realized >= profit_lock - 1e-12
            and len(native_closed) >= int(self.settings.pnl_guard_min_closed_positions_for_profit_lock)
        ):
            breaches.append(
                PnlGuardBreach(
                    code="profit_lock",
                    severity="BLOCK",
                    message=(
                        f"Session realized PnL reached {native_realized:.6f} {native_currency}; profit-lock threshold is {profit_lock:.6f} {native_currency}."
                    ),
                    value=round(native_realized, 6),
                    threshold=round(profit_lock, 6),
                    currency=native_currency,
                    context={"window_hours": resolved_window_hours},
                )
            )

        max_loss_streak = max(int(self.settings.pnl_guard_max_recent_loss_streak), 0)
        if max_loss_streak > 0 and recent_loss_streak >= max_loss_streak:
            breaches.append(
                PnlGuardBreach(
                    code="recent_loss_streak",
                    severity="HIGH_RISK",
                    message=(
                        f"Recent closed positions contain a {recent_loss_streak}-trade loss streak, above the configured threshold of {max_loss_streak}."
                    ),
                    value=recent_loss_streak,
                    threshold=max_loss_streak,
                    currency=native_currency,
                    context={"window_hours": resolved_window_hours},
                )
            )

        worst = _worst_breach(breaches)
        top_winner = None
        top_loser = None
        if realized_by_collection:
            sorted_collections = sorted(realized_by_collection.items(), key=lambda item: (item[1], item[0]))
            if sorted_collections and sorted_collections[-1][1] > 0:
                top_winner = sorted_collections[-1]
            if sorted_collections and sorted_collections[0][1] < 0:
                top_loser = sorted_collections[0]

        summary = PnlGuardSummary(
            wallet=resolved_wallet,
            chain=resolved_chain,
            currency=native_currency,
            window_hours=resolved_window_hours,
            severity=worst.severity if worst else "OK",
            block_live_submits=(worst.severity == "BLOCK") if worst else False,
            auto_force_dry_run_applied=False,
            breach_count=len(breaches),
            closed_position_count=len(native_closed),
            winning_closed_count=winning_closed_count,
            losing_closed_count=losing_closed_count,
            breakeven_closed_count=breakeven_closed_count,
            recent_loss_streak=recent_loss_streak,
            realized_pnl_native=native_realized,
            latest_exit_time=latest_exit_time,
            top_breach_code=worst.code if worst else None,
            top_breach_message=worst.message if worst else None,
            caution_loss_threshold=caution_threshold,
            loss_limit=(-loss_limit) if loss_limit is not None else None,
            profit_lock_threshold=profit_lock,
            top_winning_collection=top_winner[0] if top_winner else None,
            top_winning_pnl=top_winner[1] if top_winner else None,
            top_losing_collection=top_loser[0] if top_loser else None,
            top_losing_pnl=top_loser[1] if top_loser else None,
            realized_pnl_by_currency=realized_by_currency,
            notes=tuple(dict.fromkeys(notes)),
        )
        return PnlGuardReport(
            generated_at=now.isoformat(),
            wallet=resolved_wallet,
            chain=resolved_chain,
            currency=native_currency,
            summary=summary,
            breaches=_sorted_breaches(breaches),
        )

    def evaluate_and_apply(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
    ) -> PnlGuardReport:
        report = self.build_report(
            wallet=wallet,
            reference_limit=reference_limit,
            chain=chain,
            window_hours=window_hours,
        )
        applied = False
        if self.settings.pnl_guard_enabled and self.settings.pnl_guard_auto_force_dry_run and report.summary.block_live_submits:
            reason = f"pnl_guard:{report.summary.top_breach_code or 'block'}"
            self.state.set_force_dry_run(True, reason=reason)
            applied = True
        report.summary.auto_force_dry_run_applied = applied
        self._persist_runtime_summary(report)
        return report

    def write_report(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
        window_hours: int | None = None,
        report_path: Path | None = None,
        apply_guardrails: bool = False,
    ) -> str:
        report = (
            self.evaluate_and_apply(
                wallet=wallet,
                reference_limit=reference_limit,
                chain=chain,
                window_hours=window_hours,
            )
            if apply_guardrails
            else self.build_report(
                wallet=wallet,
                reference_limit=reference_limit,
                chain=chain,
                window_hours=window_hours,
            )
        )
        target = report_path or self.settings.pnl_guard_report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)

    def _persist_runtime_summary(self, report: PnlGuardReport) -> None:
        self.state.set_runtime_value("last_pnl_guard_at", report.generated_at)
        self.state.set_runtime_value("last_pnl_guard_severity", report.summary.severity)
        self.state.set_runtime_value("last_pnl_guard_block", "1" if report.summary.block_live_submits else "0")
        self.state.set_runtime_value("last_pnl_guard_reason", report.summary.top_breach_code)
        self.state.set_runtime_value("last_pnl_guard_reason_text", report.summary.top_breach_message)
        self.state.set_runtime_value(
            "last_pnl_guard_realized_native",
            f"{float(report.summary.realized_pnl_native):.6f}" if report.summary.realized_pnl_native is not None else None,
        )
        self.state.set_runtime_value("last_pnl_guard_currency", report.summary.currency)
        self.state.set_runtime_value("last_pnl_guard_closed_count", str(report.summary.closed_position_count))


def format_pnl_guard_text(report: PnlGuardReport, *, limit: int = 5) -> str:
    summary = report.summary
    lines = ["pnl_guard"]
    lines.append(f"wallet={summary.wallet or 'not_configured'}")
    lines.append(f"chain={summary.chain} currency={summary.currency} window_hours={summary.window_hours}")
    lines.append(f"severity={summary.severity}")
    lines.append(
        f"block_live_submits={str(summary.block_live_submits).lower()} auto_force_dry_run_applied={str(summary.auto_force_dry_run_applied).lower()}"
    )
    lines.append(
        f"closed_positions={summary.closed_position_count} wins={summary.winning_closed_count} losses={summary.losing_closed_count} breakeven={summary.breakeven_closed_count} loss_streak={summary.recent_loss_streak}"
    )
    realized_text = _format_breakdown(summary.realized_pnl_by_currency)
    native_text = f"{summary.realized_pnl_native:+.6f} {summary.currency}" if summary.realized_pnl_native is not None else "n/a"
    lines.append(f"realized_pnl={realized_text} native={native_text}")
    if summary.loss_limit is not None:
        lines.append(f"loss_limit={summary.loss_limit:.6f} {summary.currency}")
    if summary.caution_loss_threshold is not None:
        lines.append(f"caution_threshold={summary.caution_loss_threshold:.6f} {summary.currency}")
    if summary.profit_lock_threshold is not None:
        lines.append(f"profit_lock_threshold={summary.profit_lock_threshold:.6f} {summary.currency}")
    if summary.top_winning_collection:
        lines.append(f"top_winner={summary.top_winning_collection} {summary.top_winning_pnl:+.6f} {summary.currency}")
    if summary.top_losing_collection:
        lines.append(f"top_loser={summary.top_losing_collection} {summary.top_losing_pnl:+.6f} {summary.currency}")
    if summary.latest_exit_time:
        lines.append(f"latest_exit_time={summary.latest_exit_time}")
    if summary.top_breach_code:
        lines.append(f"top_breach={summary.top_breach_code}")
    if summary.top_breach_message:
        lines.append(f"top_message={summary.top_breach_message}")
    if summary.notes:
        lines.append(f"notes={','.join(summary.notes)}")
    if report.breaches:
        lines.append("breaches:")
        for breach in report.breaches[: max(int(limit), 0)]:
            extra = []
            if breach.currency:
                extra.append(breach.currency)
            if breach.threshold is not None:
                extra.append(f"threshold={breach.threshold}")
            if breach.value is not None:
                extra.append(f"value={breach.value}")
            suffix = f" [{' | '.join(extra)}]" if extra else ""
            lines.append(f"- {breach.severity} {breach.code}: {breach.message}{suffix}")
    return "\n".join(lines)


def _native_currency_for_chain(chain: str) -> str:
    return _NATIVE_CURRENCY_BY_CHAIN.get(chain.strip().lower(), "BNB")


def _parse_dt(raw_value: str | None) -> datetime | None:
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


def _recent_loss_streak(closed_positions: list[Any]) -> int:
    streak = 0
    for item in closed_positions:
        pnl = float(item.realized_pnl)
        if pnl < -1e-12:
            streak += 1
            continue
        break
    return streak


def _format_breakdown(values: dict[str, float]) -> str:
    if not values:
        return "n/a"
    return ", ".join(f"{currency}:{float(amount):+.6f}" for currency, amount in sorted(values.items()))


def _worst_breach(breaches: list[PnlGuardBreach]) -> PnlGuardBreach | None:
    if not breaches:
        return None
    return _sorted_breaches(breaches)[0]


def _sorted_breaches(breaches: list[PnlGuardBreach]) -> list[PnlGuardBreach]:
    return sorted(
        breaches,
        key=lambda item: (-_SEVERITY_RANK.get(item.severity, 0), str(item.code), str(item.currency or "")),
    )
