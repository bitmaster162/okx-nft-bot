from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest

from okx_nft_bot import execution_daemon


_EFFECT_FLAGS = (
    "EXECUTION_DAEMON_ENABLED",
    "COUNTERBID_ENABLED",
    "UNDERCUTTER_ENABLED",
)


def _clear_effect_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _EFFECT_FLAGS:
        monkeypatch.delenv(name, raising=False)


def test_execution_daemon_is_disabled_when_flag_is_missing(monkeypatch):
    _clear_effect_flags(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        execution_daemon.main()

    assert exc.value.code == 0


def test_effect_engines_are_opt_in_when_daemon_is_explicitly_enabled(monkeypatch):
    _clear_effect_flags(monkeypatch)
    monkeypatch.setenv("EXECUTION_DAEMON_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_SCAN_INTERVAL", "1")

    fake_config = ModuleType("okx_nft_bot.config")
    fake_config.load_settings = lambda: object()
    monkeypatch.setitem(sys.modules, "okx_nft_bot.config", fake_config)

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {
            "okx_nft_bot.sniper.counter_bidder",
            "okx_nft_bot.undercutter.engine",
        }:
            pytest.fail(f"effect engine imported without explicit opt-in: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    class StopAfterSafeCycle(RuntimeError):
        pass

    def stop_after_safe_cycle(_seconds):
        raise StopAfterSafeCycle

    monkeypatch.setattr(execution_daemon.time, "sleep", stop_after_safe_cycle)

    with pytest.raises(StopAfterSafeCycle):
        execution_daemon.main()
