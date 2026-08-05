"""Shadow advisor — minimal rule-based RuleEngine port from SWE-1.6 decision_engine.

Standalone, pure-Python, no sklearn/joblib deps. Runs in shadow mode only:
when ML_ADVISOR_ENABLED=1, the bot logs advisor recommendations next to
actual decisions but does NOT act on them. Set to 0 to disable completely.

Future: extend to load .pkl ML models from data/models/ when joblib/sklearn
become available in the prod container."""
from __future__ import annotations
import os, time, logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


class ActionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CANCEL = "cancel"


@dataclass
class MarketContext:
    chain: str = "bsc"
    collection: str = ""
    wallet: str = ""
    current_position: float = 0.0       # qty we hold
    portfolio_value: float = 0.0        # $ total exposure
    fraud_score: float = 0.0            # 0..1
    price_trend: str = "stable"         # up/down/stable
    price_confidence: float = 0.5
    volume_24h: float = 0.0
    binance_price: float = 0.0
    binance_signal: str = "hold"
    cross_platform_spread: float = 0.0  # pct
    competitor_activity: int = 0
    bnb_price: float = 580.0
    is_wl: bool = True


@dataclass
class Recommendation:
    action: ActionType
    confidence: float
    reason: str
    rule: str
    ts: str = ""


class RuleEngine:
    """Heuristic rules — same logic as SWE-1.6 RuleEngine, no ML."""
    def __init__(self) -> None:
        self.rules = [
            self._high_fraud_rule,
            self._cross_platform_arb_rule,
            self._uptrend_rule,
            self._downtrend_rule,
            self._binance_signal_rule,
            self._binance_whitelist_rule,
            self._competitor_active_rule,
        ]

    def evaluate(self, ctx: MarketContext) -> Recommendation:
        best = Recommendation(ActionType.HOLD, 0.0, "no signal", "default", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        for rule in self.rules:
            try:
                r = rule(ctx)
                if r and r.confidence > best.confidence:
                    best = r
            except Exception as exc:
                log.warning("[ADVISOR] rule %s error: %s", getattr(rule, "__name__", "?"), exc)
        return best

    def _high_fraud_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.fraud_score > 0.7:
            return Recommendation(ActionType.HOLD, 0.9, f"fraud={ctx.fraud_score:.2f} > 0.7", "high_fraud", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _cross_platform_arb_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if abs(ctx.cross_platform_spread) >= 20:
            direction = ActionType.BUY if ctx.cross_platform_spread > 0 else ActionType.SELL
            return Recommendation(direction, 0.75,
                f"cross-platform spread {ctx.cross_platform_spread:+.1f}% on {ctx.collection}",
                "cross_arb", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _uptrend_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.price_trend == "up" and ctx.price_confidence > 0.6:
            return Recommendation(ActionType.BUY, 0.7,
                f"uptrend conf={ctx.price_confidence:.2f}", "uptrend", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _downtrend_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.price_trend == "down" and ctx.price_confidence > 0.6:
            return Recommendation(ActionType.SELL, 0.7,
                f"downtrend conf={ctx.price_confidence:.2f}", "downtrend", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _binance_signal_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.binance_signal == "buy":
            return Recommendation(ActionType.BUY, 0.6, "binance signal=buy", "binance_signal", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if ctx.binance_signal == "sell":
            return Recommendation(ActionType.SELL, 0.6, "binance signal=sell", "binance_signal", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _binance_whitelist_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.is_wl and ctx.competitor_activity > 0:
            return Recommendation(ActionType.BUY, 0.55,
                "WL collection with rivals — undercut opportunity", "wl_capture", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None

    def _competitor_active_rule(self, ctx: MarketContext) -> Optional[Recommendation]:
        if ctx.competitor_activity >= 5:
            return Recommendation(ActionType.HOLD, 0.5,
                f"high competitor activity ({ctx.competitor_activity}) — wait", "comp_active", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return None


_ENGINE = RuleEngine()


def advise(ctx: MarketContext) -> Recommendation:
    """Public entry point. Returns Recommendation. Never raises."""
    try:
        return _ENGINE.evaluate(ctx)
    except Exception as exc:
        log.warning("[ML_ADVISOR] evaluate error: %s", exc)
        return Recommendation(ActionType.HOLD, 0.0, f"err: {exc}", "error", time.strftime("%Y-%m-%dT%H:%M:%SZ"))


def is_enabled() -> bool:
    return os.environ.get("ML_ADVISOR_ENABLED", "0") in ("1", "true", "yes", "on")
