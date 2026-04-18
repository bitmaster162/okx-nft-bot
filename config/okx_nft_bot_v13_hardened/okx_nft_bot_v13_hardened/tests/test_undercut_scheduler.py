from __future__ import annotations

from pathlib import Path

import pytest

from okx_nft_bot.config import Settings
from okx_nft_bot.undercutter.engine import UndercutAction
from okx_nft_bot.undercutter.scheduler import UndercutScheduler


class FakeState:
    def __init__(self) -> None:
        self.cleanup_calls: list[int] = []

    def cleanup_stale_offers(self, *, max_age_days: int = 7) -> int:
        self.cleanup_calls.append(max_age_days)
        return 0


class FakeGovernor:
    def __init__(self) -> None:
        self.reconcile_calls: list[str | None] = []

    def reconcile_active_offers(self, *, chain: str | None = None):
        self.reconcile_calls.append(chain)

        class _Result:
            local_marked_missing = 0
            local_added_from_exchange = 0
            exchange_seen = 0

        return _Result()


class FakeEngine:
    def __init__(self, *, stop_after: int | None = None) -> None:
        self.state = FakeState()
        self.governor = FakeGovernor()
        self.calls = 0
        self.stop_after = stop_after

    def run_cycle(self, *, chain: str | None = None, refresh: bool = False) -> list[UndercutAction]:
        _ = chain, refresh
        if self.stop_after is not None and self.calls >= self.stop_after:
            raise StopIteration("stop infinite daemon in test")
        self.calls += 1
        return [
            UndercutAction(
                action_type="ATTACK",
                collection="0xabc",
                chain="bsc",
                old_price_bnb=None,
                new_price_bnb=0.5,
                reason="test",
                executed=True,
            )
        ]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_chain="bsc",
        dry_run=True,
        undercut_interval_seconds=30,
    )


def test_run_daemon_returns_requested_number_of_cycles(tmp_path: Path) -> None:
    engine = FakeEngine()
    scheduler = UndercutScheduler(engine=engine, settings=_settings(tmp_path))

    result = scheduler.run_daemon(cycles=3, interval_seconds=0, chain="bsc")

    assert result.cycles == 3
    assert len(result.runs) == 3
    assert engine.calls == 3
    assert result.interval_seconds == 0


def test_run_daemon_treats_zero_cycles_as_infinite(tmp_path: Path) -> None:
    engine = FakeEngine(stop_after=5)
    scheduler = UndercutScheduler(engine=engine, settings=_settings(tmp_path))

    with pytest.raises(StopIteration, match="stop infinite daemon in test"):
        scheduler.run_daemon(cycles=0, interval_seconds=0, chain="bsc")

    assert engine.calls == 5


def test_run_daemon_triggers_periodic_cleanup(tmp_path: Path) -> None:
    engine = FakeEngine()
    scheduler = UndercutScheduler(engine=engine, settings=_settings(tmp_path))

    result = scheduler.run_daemon(cycles=100, interval_seconds=0, chain="bsc")

    assert result.cycles == 100
    assert engine.state.cleanup_calls == [7]


def test_run_daemon_triggers_periodic_reconciliation(tmp_path: Path) -> None:
    engine = FakeEngine()
    scheduler = UndercutScheduler(engine=engine, settings=_settings(tmp_path))

    result = scheduler.run_daemon(cycles=40, interval_seconds=0, chain="bsc")

    assert result.cycles == 40
    assert engine.governor.reconcile_calls == ["bsc", "bsc"]


def test_run_daemon_checks_health_alerts_when_channels_configured(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.telegram_bot_token = "token"
    settings.telegram_chat_id = "123"
    engine = FakeEngine()
    scheduler = UndercutScheduler(engine=engine, settings=settings)
    calls: list[str] = []
    monkeypatch.setattr("okx_nft_bot.undercutter.scheduler.maybe_send_health_alert", lambda settings, *, source: calls.append(source))

    result = scheduler.run_daemon(cycles=3, interval_seconds=0, chain="bsc")

    assert result.cycles == 3
    assert calls == ["undercut_daemon", "undercut_daemon", "undercut_daemon"]
