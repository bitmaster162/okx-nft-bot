from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from okx_nft_bot.analytics.portfolio import WalletPnlAnalyzer, WalletPnlReport
from okx_nft_bot.config import Settings
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.undercutter.state import PositionState


_SEVERITY_RANK = {
    "OK": 0,
    "CAUTION": 1,
    "HIGH_RISK": 2,
    "BLOCK": 3,
}


@dataclass(slots=True)
class PortfolioRiskBreach:
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
class PortfolioRiskSummary:
    wallet: str | None
    chain: str
    severity: str
    block_live_submits: bool
    auto_force_dry_run_applied: bool
    breach_count: int
    active_offer_count: int
    active_exposure_bnb: float
    active_exposure_cap_bnb: float | None
    open_position_count: int
    priced_open_position_count: int
    unpriced_open_position_count: int
    unpriced_open_ratio: float
    orphan_sale_count: int
    killswitch_failed_count: int
    reconcile_age_seconds: float | None
    fill_reconcile_age_seconds: float | None
    top_breach_code: str | None
    top_breach_message: str | None
    inventory_drawdown_by_currency: dict[str, float] = field(default_factory=dict)
    latest_trade_at: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "chain": self.chain,
            "severity": self.severity,
            "block_live_submits": self.block_live_submits,
            "auto_force_dry_run_applied": self.auto_force_dry_run_applied,
            "breach_count": self.breach_count,
            "active_offer_count": self.active_offer_count,
            "active_exposure_bnb": round(self.active_exposure_bnb, 6),
            "active_exposure_cap_bnb": round(self.active_exposure_cap_bnb, 6) if self.active_exposure_cap_bnb is not None else None,
            "open_position_count": self.open_position_count,
            "priced_open_position_count": self.priced_open_position_count,
            "unpriced_open_position_count": self.unpriced_open_position_count,
            "unpriced_open_ratio": round(self.unpriced_open_ratio, 4),
            "orphan_sale_count": self.orphan_sale_count,
            "killswitch_failed_count": self.killswitch_failed_count,
            "reconcile_age_seconds": round(self.reconcile_age_seconds, 2) if self.reconcile_age_seconds is not None else None,
            "fill_reconcile_age_seconds": round(self.fill_reconcile_age_seconds, 2) if self.fill_reconcile_age_seconds is not None else None,
            "top_breach_code": self.top_breach_code,
            "top_breach_message": self.top_breach_message,
            "inventory_drawdown_by_currency": {key: round(value, 4) for key, value in sorted(self.inventory_drawdown_by_currency.items())},
            "latest_trade_at": self.latest_trade_at,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class PortfolioRiskReport:
    generated_at: str
    wallet: str | None
    chain: str
    summary: PortfolioRiskSummary
    breaches: list[PortfolioRiskBreach]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "wallet": self.wallet,
            "chain": self.chain,
            "summary": self.summary.to_dict(),
            "breaches": [item.to_dict() for item in self.breaches],
        }


class PortfolioRiskAnalyzer:
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
    ) -> PortfolioRiskReport:
        resolved_chain = (chain or self.settings.execution_chain).strip().lower()
        resolved_wallet = (wallet or self.settings.buyer_wallet_address or "").strip().lower() or None
        pnl_report = WalletPnlAnalyzer(settings=self.settings, store=self.store).build_report(
            wallet=resolved_wallet,
            reference_limit=reference_limit or self.settings.wallet_pnl_reference_event_limit,
            collection_limit=10,
            open_limit=50,
            closed_limit=100,
        )
        now = datetime.now(timezone.utc)
        runtime = self.state.get_runtime_state()
        active_offer_count = self.state.count_active_offers(chain=resolved_chain)
        active_exposure_bnb = self.state.sum_active_exposure_bnb(chain=resolved_chain)
        killswitch_failed_count = len(self.state.get_killswitch_failed_offers(chain=resolved_chain))
        open_count = pnl_report.summary.open_position_count
        priced_open_count = pnl_report.summary.priced_open_position_count
        unpriced_open_count = max(open_count - priced_open_count, 0)
        unpriced_open_ratio = (unpriced_open_count / open_count) if open_count else 0.0
        reconcile_age_seconds = _age_seconds(runtime.get("last_reconcile_at"), now)
        fill_reconcile_age_seconds = _age_seconds(runtime.get("last_fill_reconcile_at"), now)
        exposure_cap = self.settings.portfolio_risk_max_active_exposure_bnb
        if exposure_cap is None or exposure_cap <= 0:
            exposure_cap = max(float(self.settings.max_bnb_per_day) * 1.2, float(self.settings.mass_offer_price_bnb))

        breaches: list[PortfolioRiskBreach] = []
        drawdown_by_currency = _inventory_drawdowns(pnl_report)

        if not resolved_wallet:
            breaches.append(
                PortfolioRiskBreach(
                    code="wallet_not_configured",
                    severity="CAUTION",
                    message="Buyer wallet is not configured; portfolio guardrails are running blind.",
                )
            )

        if killswitch_failed_count > int(self.settings.portfolio_risk_max_killswitch_failed):
            breaches.append(
                PortfolioRiskBreach(
                    code="killswitch_failed",
                    severity="BLOCK",
                    message=(
                        f"Killswitch has {killswitch_failed_count} failed live offer(s); new live submits must remain blocked."
                    ),
                    value=killswitch_failed_count,
                    threshold=int(self.settings.portfolio_risk_max_killswitch_failed),
                )
            )

        if active_exposure_bnb > float(exposure_cap) + 1e-12:
            breaches.append(
                PortfolioRiskBreach(
                    code="active_exposure_limit",
                    severity="BLOCK",
                    message=(
                        f"Active offer exposure is {active_exposure_bnb:.6f} BNB, above the guardrail cap of {float(exposure_cap):.6f} BNB."
                    ),
                    value=round(active_exposure_bnb, 6),
                    threshold=round(float(exposure_cap), 6),
                )
            )

        orphan_sale_count = int(pnl_report.summary.orphan_sale_count)
        if orphan_sale_count > int(self.settings.portfolio_risk_max_orphan_sales):
            breaches.append(
                PortfolioRiskBreach(
                    code="orphan_sales",
                    severity="HIGH_RISK",
                    message=(
                        f"Wallet history has {orphan_sale_count} orphan sale(s), above the allowed maximum of {int(self.settings.portfolio_risk_max_orphan_sales)}."
                    ),
                    value=orphan_sale_count,
                    threshold=int(self.settings.portfolio_risk_max_orphan_sales),
                )
            )
        elif orphan_sale_count > 0:
            breaches.append(
                PortfolioRiskBreach(
                    code="orphan_sales_present",
                    severity="CAUTION",
                    message=f"Wallet history still contains {orphan_sale_count} orphan sale(s).",
                    value=orphan_sale_count,
                    threshold=int(self.settings.portfolio_risk_max_orphan_sales),
                )
            )

        if unpriced_open_count >= int(self.settings.portfolio_risk_max_unpriced_positions) and open_count > 0:
            breaches.append(
                PortfolioRiskBreach(
                    code="unpriced_open_positions",
                    severity="HIGH_RISK",
                    message=(
                        f"There are {unpriced_open_count} unpriced open position(s), above the guardrail cap of {int(self.settings.portfolio_risk_max_unpriced_positions)}."
                    ),
                    value=unpriced_open_count,
                    threshold=int(self.settings.portfolio_risk_max_unpriced_positions),
                )
            )
        if open_count >= 3 and unpriced_open_ratio > float(self.settings.portfolio_risk_max_unpriced_ratio) + 1e-12:
            breaches.append(
                PortfolioRiskBreach(
                    code="unpriced_open_ratio",
                    severity="HIGH_RISK",
                    message=(
                        f"Unpriced open inventory ratio is {unpriced_open_ratio * 100.0:.1f}%, above the guardrail cap of {float(self.settings.portfolio_risk_max_unpriced_ratio) * 100.0:.1f}%."
                    ),
                    value=round(unpriced_open_ratio, 4),
                    threshold=float(self.settings.portfolio_risk_max_unpriced_ratio),
                )
            )

        min_inventory_cost = float(self.settings.portfolio_risk_min_inventory_cost)
        max_drawdown_pct = float(self.settings.portfolio_risk_max_drawdown_pct)
        for currency, drawdown in sorted(drawdown_by_currency.items()):
            inventory_cost = float(pnl_report.summary.inventory_cost_by_currency.get(currency) or 0.0)
            if inventory_cost < min_inventory_cost:
                continue
            if drawdown > max_drawdown_pct + 1e-12:
                breaches.append(
                    PortfolioRiskBreach(
                        code="inventory_drawdown",
                        severity="BLOCK",
                        message=(
                            f"Open inventory drawdown for {currency} is {drawdown * 100.0:.1f}% on {inventory_cost:.6f} {currency} cost basis, above the {max_drawdown_pct * 100.0:.1f}% cap."
                        ),
                        value=round(drawdown, 4),
                        threshold=max_drawdown_pct,
                        currency=currency,
                    )
                )
            elif drawdown > max_drawdown_pct * 0.75 + 1e-12:
                breaches.append(
                    PortfolioRiskBreach(
                        code="inventory_drawdown_warning",
                        severity="HIGH_RISK",
                        message=(
                            f"Open inventory drawdown for {currency} is already {drawdown * 100.0:.1f}% on {inventory_cost:.6f} {currency} cost basis."
                        ),
                        value=round(drawdown, 4),
                        threshold=max_drawdown_pct,
                        currency=currency,
                    )
                )

        max_reconcile_age = max(int(self.settings.portfolio_risk_max_reconcile_age_seconds), 0)
        if active_offer_count > 0:
            if reconcile_age_seconds is None:
                breaches.append(
                    PortfolioRiskBreach(
                        code="execution_never_reconciled",
                        severity="HIGH_RISK",
                        message="Execution state has active offers but no successful reconcile timestamp.",
                    )
                )
            elif max_reconcile_age > 0:
                if reconcile_age_seconds > max_reconcile_age * 2:
                    breaches.append(
                        PortfolioRiskBreach(
                            code="execution_reconcile_stale",
                            severity="BLOCK",
                            message=(
                                f"Execution reconcile is stale at {reconcile_age_seconds:.0f}s with live active offers present."
                            ),
                            value=round(reconcile_age_seconds, 2),
                            threshold=max_reconcile_age,
                        )
                    )
                elif reconcile_age_seconds > max_reconcile_age:
                    breaches.append(
                        PortfolioRiskBreach(
                            code="execution_reconcile_warning",
                            severity="HIGH_RISK",
                            message=(
                                f"Execution reconcile age is {reconcile_age_seconds:.0f}s, beyond the {max_reconcile_age}s freshness window."
                            ),
                            value=round(reconcile_age_seconds, 2),
                            threshold=max_reconcile_age,
                        )
                    )

            max_fill_age = max(int(self.settings.portfolio_risk_max_fill_reconcile_age_seconds), 0)
            if fill_reconcile_age_seconds is None:
                breaches.append(
                    PortfolioRiskBreach(
                        code="fill_reconcile_missing",
                        severity="CAUTION",
                        message="Execution fill reconciliation has never been run.",
                    )
                )
            elif max_fill_age > 0 and fill_reconcile_age_seconds > max_fill_age:
                severity = "HIGH_RISK" if fill_reconcile_age_seconds > max_fill_age * 2 else "CAUTION"
                breaches.append(
                    PortfolioRiskBreach(
                        code="fill_reconcile_stale",
                        severity=severity,
                        message=(
                            f"Execution fill reconciliation age is {fill_reconcile_age_seconds:.0f}s, beyond the {max_fill_age}s freshness window."
                        ),
                        value=round(fill_reconcile_age_seconds, 2),
                        threshold=max_fill_age,
                    )
                )

        worst_breach = _worst_breach(breaches)
        severity = worst_breach.severity if worst_breach else "OK"
        summary = PortfolioRiskSummary(
            wallet=resolved_wallet,
            chain=resolved_chain,
            severity=severity,
            block_live_submits=severity == "BLOCK",
            auto_force_dry_run_applied=False,
            breach_count=len(breaches),
            active_offer_count=active_offer_count,
            active_exposure_bnb=active_exposure_bnb,
            active_exposure_cap_bnb=float(exposure_cap) if exposure_cap is not None else None,
            open_position_count=open_count,
            priced_open_position_count=priced_open_count,
            unpriced_open_position_count=unpriced_open_count,
            unpriced_open_ratio=unpriced_open_ratio,
            orphan_sale_count=orphan_sale_count,
            killswitch_failed_count=killswitch_failed_count,
            reconcile_age_seconds=reconcile_age_seconds,
            fill_reconcile_age_seconds=fill_reconcile_age_seconds,
            top_breach_code=worst_breach.code if worst_breach else None,
            top_breach_message=worst_breach.message if worst_breach else None,
            inventory_drawdown_by_currency=drawdown_by_currency,
            latest_trade_at=pnl_report.summary.latest_trade_at,
            notes=tuple(pnl_report.summary.notes),
        )
        return PortfolioRiskReport(
            generated_at=now.isoformat(),
            wallet=resolved_wallet,
            chain=resolved_chain,
            summary=summary,
            breaches=_sorted_breaches(breaches),
        )

    def evaluate_and_apply(
        self,
        *,
        wallet: str | None = None,
        reference_limit: int | None = None,
        chain: str | None = None,
    ) -> PortfolioRiskReport:
        report = self.build_report(wallet=wallet, reference_limit=reference_limit, chain=chain)
        applied = False
        if self.settings.portfolio_risk_enabled and self.settings.portfolio_risk_auto_force_dry_run and report.summary.block_live_submits:
            reason = f"portfolio_risk:{report.summary.top_breach_code or 'block'}"
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
        report_path: Path | None = None,
        apply_guardrails: bool = False,
    ) -> str:
        report = (
            self.evaluate_and_apply(wallet=wallet, reference_limit=reference_limit, chain=chain)
            if apply_guardrails
            else self.build_report(wallet=wallet, reference_limit=reference_limit, chain=chain)
        )
        target = report_path or self.settings.portfolio_risk_report_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)

    def _persist_runtime_summary(self, report: PortfolioRiskReport) -> None:
        self.state.set_runtime_value("last_portfolio_risk_at", report.generated_at)
        self.state.set_runtime_value("last_portfolio_risk_severity", report.summary.severity)
        self.state.set_runtime_value("last_portfolio_risk_block", "1" if report.summary.block_live_submits else "0")
        self.state.set_runtime_value("last_portfolio_risk_reason", report.summary.top_breach_code)
        self.state.set_runtime_value(
            "last_portfolio_risk_reason_text",
            report.summary.top_breach_message,
        )
        self.state.set_runtime_value(
            "last_portfolio_risk_breach_count",
            str(report.summary.breach_count),
        )


def format_portfolio_risk_text(report: PortfolioRiskReport, *, limit: int = 5) -> str:
    summary = report.summary
    lines = ["portfolio_risk"]
    lines.append(f"wallet={summary.wallet or 'not_configured'}")
    lines.append(f"chain={summary.chain}")
    lines.append(f"severity={summary.severity}")
    lines.append(
        f"block_live_submits={str(summary.block_live_submits).lower()} auto_force_dry_run_applied={str(summary.auto_force_dry_run_applied).lower()}"
    )
    lines.append(
        f"active_offers={summary.active_offer_count} active_exposure_bnb={summary.active_exposure_bnb:.6f} cap_bnb={summary.active_exposure_cap_bnb:.6f}" if summary.active_exposure_cap_bnb is not None else f"active_offers={summary.active_offer_count} active_exposure_bnb={summary.active_exposure_bnb:.6f}"
    )
    lines.append(
        f"open_positions={summary.open_position_count} priced_open={summary.priced_open_position_count} unpriced_open={summary.unpriced_open_position_count} orphan_sales={summary.orphan_sale_count}"
    )
    lines.append(f"unpriced_open_ratio={summary.unpriced_open_ratio * 100.0:.1f}%")
    lines.append(f"killswitch_failed={summary.killswitch_failed_count}")
    if summary.reconcile_age_seconds is not None:
        lines.append(f"reconcile_age_seconds={summary.reconcile_age_seconds:.0f}")
    if summary.fill_reconcile_age_seconds is not None:
        lines.append(f"fill_reconcile_age_seconds={summary.fill_reconcile_age_seconds:.0f}")
    if summary.inventory_drawdown_by_currency:
        drawdowns = ", ".join(
            f"{currency}:{drawdown * 100.0:.1f}%" for currency, drawdown in sorted(summary.inventory_drawdown_by_currency.items())
        )
        lines.append(f"inventory_drawdown={drawdowns}")
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


def _inventory_drawdowns(report: WalletPnlReport) -> dict[str, float]:
    drawdowns: dict[str, float] = {}
    for currency, raw_cost in report.summary.inventory_cost_by_currency.items():
        cost = float(raw_cost or 0.0)
        if cost <= 0:
            continue
        raw_value = report.summary.inventory_value_by_currency.get(currency)
        if raw_value is None:
            continue
        value = float(raw_value)
        drawdowns[currency] = max((cost - value) / cost, 0.0)
    return drawdowns


def _age_seconds(raw_value: str | None, now: datetime) -> float | None:
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
    return max((now - parsed).total_seconds(), 0.0)


def _worst_breach(breaches: list[PortfolioRiskBreach]) -> PortfolioRiskBreach | None:
    if not breaches:
        return None
    return _sorted_breaches(breaches)[0]


def _sorted_breaches(breaches: list[PortfolioRiskBreach]) -> list[PortfolioRiskBreach]:
    return sorted(
        breaches,
        key=lambda item: (-_SEVERITY_RANK.get(item.severity, 0), str(item.code), str(item.currency or "")),
    )
