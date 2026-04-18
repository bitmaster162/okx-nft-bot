from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from okx_nft_bot.config import Settings
from okx_nft_bot.ops import maybe_send_health_alert
from okx_nft_bot.undercutter.engine import UndercutAction, UndercutEngine

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DaemonResult:
    cycles: int
    interval_seconds: int
    runs: list[list[dict[str, Any]]]


class UndercutScheduler:
    def __init__(self, engine: UndercutEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def run_once(self, *, chain: str | None = None, refresh: bool = False) -> list[UndercutAction]:
        return self.engine.run_cycle(chain=chain, refresh=refresh)

    def run_daemon(
        self,
        *,
        cycles: int,
        interval_seconds: int | None = None,
        chain: str | None = None,
        refresh: bool = False,
    ) -> DaemonResult:
        resolved_interval = self.settings.undercut_interval_seconds if interval_seconds is None else interval_seconds
        runs: list[list[dict[str, Any]]] = []
        index = 0
        while True:
            actions = self.engine.run_cycle(chain=chain, refresh=refresh)
            runs.append([asdict(action) for action in actions])
            index += 1
            if self.settings.telegram_bot_token or self.settings.webhook_url:
                maybe_send_health_alert(self.settings, source='undercut_daemon')
            if index % 20 == 0 and hasattr(self.engine, "governor"):
                try:
                    result = self.engine.governor.reconcile_active_offers(chain=chain)
                    if result.local_marked_missing or result.local_added_from_exchange:
                        logger.info(
                            "Reconciled execution state: missing=%d added=%d exchange_seen=%d",
                            result.local_marked_missing,
                            result.local_added_from_exchange,
                            result.exchange_seen,
                        )
                except Exception as exc:
                    logger.warning("Execution reconciliation failed: %s", exc)
            if index % 100 == 0:
                cleaned = self.engine.state.cleanup_stale_offers(max_age_days=7)
                if cleaned:
                    logger.info("Cleaned up %d stale offers from active_offers", cleaned)
            if cycles > 0 and index >= cycles:
                break
            if resolved_interval > 0:
                time.sleep(resolved_interval)
        return DaemonResult(
            cycles=index,
            interval_seconds=resolved_interval,
            runs=runs,
        )