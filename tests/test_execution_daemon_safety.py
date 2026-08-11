from __future__ import annotations

import builtins
import signal
import sys
from types import ModuleType

import pytest

from okx_nft_bot import execution_daemon


_EFFECT_FLAGS = (
    "EXECUTION_DAEMON_ENABLED",
    "COUNTERBID_ENABLED",
    "UNDERCUTTER_ENABLED",
    "UNDERCUTTER_CHAINS",
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
    monkeypatch.setattr(execution_daemon, "_shutdown_requested", False)

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


def test_shutdown_handler_marks_request(monkeypatch):
    monkeypatch.setattr(execution_daemon, "_shutdown_requested", False)

    execution_daemon._handle_shutdown(signal.SIGTERM, None)

    assert execution_daemon._shutdown_requested is True


def test_shutdown_aware_sleep_returns_without_sleep_when_requested(monkeypatch):
    monkeypatch.setattr(execution_daemon, "_shutdown_requested", True)

    def unexpected_sleep(_seconds):
        pytest.fail("sleep called after shutdown request")

    monkeypatch.setattr(execution_daemon.time, "sleep", unexpected_sleep)

    execution_daemon._sleep_until_next_cycle(60)


def test_undercutter_chains_default_to_execution_chain(monkeypatch):
    monkeypatch.delenv("UNDERCUTTER_CHAINS", raising=False)

    assert execution_daemon._configured_undercut_chains("bsc") == ("bsc",)


def test_undercutter_chains_are_explicit_multichain_and_deduplicated(monkeypatch):
    monkeypatch.setenv("UNDERCUTTER_CHAINS", "eth,bsc,eth")

    assert execution_daemon._configured_undercut_chains("bsc") == ("eth", "bsc")


def test_blank_undercutter_chains_preserve_execution_chain(monkeypatch):
    monkeypatch.setenv("UNDERCUTTER_CHAINS", "   ")

    assert execution_daemon._configured_undercut_chains("bsc") == ("bsc",)


def test_invalid_undercutter_chain_fails_closed(monkeypatch):
    monkeypatch.setenv("UNDERCUTTER_CHAINS", "bsc,solana")

    with pytest.raises(ValueError):
        execution_daemon._configured_undercut_chains("bsc")
