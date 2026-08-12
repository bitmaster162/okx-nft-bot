from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import okx_nft_bot.killswitch as killswitch


class _State:
    def __init__(self) -> None:
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def disarm_live(self, **_kwargs):
        return {"armed": False}

    def set_force_dry_run(self, _value: bool, **_kwargs):
        return None

    def set_runtime_value(self, _key: str, _value: object):
        return None

    def audit_integrity(self):
        return None

    def get_active_offers(self, *, chain: str):
        return []

    def record_submit_event(self, **kwargs):
        self.submit_events.append(dict(kwargs))
        return len(self.submit_events)

    def log_action(self, **kwargs):
        self.actions.append(dict(kwargs))
        return len(self.actions)


class _InjectedAPI:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        return [
            {
                "offerId": f"offer-{chain}",
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        self.cancelled.append(chain)
        return True


def _settings(tmp_path: Path):
    return SimpleNamespace(
        execution_db_path=tmp_path / "execution.sqlite3",
        dry_run=False,
    )


def test_api_constructor_failure_returns_structured_fatal_results(monkeypatch, tmp_path):
    class _BrokenAPI:
        def __init__(self, *, settings):
            assert settings.dry_run is True
            raise RuntimeError("OKX client init unavailable")

    monkeypatch.setattr(killswitch, "OKXAPIClient", _BrokenAPI)
    settings = _settings(tmp_path)
    state = _State()

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        state=state,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert result.preflight_error is None
    assert [item.chain for item in result.chains] == ["bsc", "eth"]
    assert [item.fatal_error for item in result.chains] == [
        "api_init: OKX client init unavailable",
        "api_init: OKX client init unavailable",
    ]
    assert result.total_failed == 2
    assert [row["chain"] for row in state.submit_events] == ["bsc", "eth"]
    assert all(row["status"] == "killswitch" for row in state.submit_events)


def test_state_and_api_constructor_failures_are_both_preserved(monkeypatch, tmp_path):
    class _BrokenState:
        def __init__(self, _db_path):
            raise RuntimeError("execution DB init unavailable")

    class _BrokenAPI:
        def __init__(self, *, settings):
            assert settings.dry_run is True
            raise RuntimeError("OKX client init unavailable")

    monkeypatch.setattr(killswitch, "PositionState", _BrokenState)
    monkeypatch.setattr(killswitch, "OKXAPIClient", _BrokenAPI)
    settings = _settings(tmp_path)

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert result.preflight_error == "state_init: execution DB init unavailable"
    assert [item.fatal_error for item in result.chains] == [
        "api_init: OKX client init unavailable",
        "api_init: OKX client init unavailable",
    ]
    assert result.total_failed == 3


def test_injected_api_bypasses_constructor_failure_path(monkeypatch, tmp_path):
    class _ForbiddenAPI:
        def __init__(self, **_kwargs):
            raise AssertionError("injected API must bypass OKXAPIClient construction")

    monkeypatch.setattr(killswitch, "OKXAPIClient", _ForbiddenAPI)
    settings = _settings(tmp_path)
    state = _State()
    api = _InjectedAPI()

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("eth",),
    )

    assert api.cancelled == ["eth"]
    assert result.preflight_error is None
    assert result.total_failed == 0
    assert result.chains[0].fatal_error is None
    assert result.chains[0].live_cancelled == 1
