from __future__ import annotations

from pathlib import Path

import okx_nft_bot.killswitch as killswitch


class _Settings:
    def __init__(self, trace: list[str], db_path: Path) -> None:
        self.trace = trace
        self.execution_db_path = db_path
        self._dry_run = False

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = bool(value)
        self.trace.append(f"settings_dry:{int(self._dry_run)}")


class _API:
    def __init__(
        self,
        *,
        cancel_result: bool = True,
        fail_lookup: bool = False,
        malformed_only: bool = False,
    ) -> None:
        self.cancel_result = cancel_result
        self.fail_lookup = fail_lookup
        self.malformed_only = malformed_only
        self.trace: list[str] = []
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        if self.fail_lookup:
            raise RuntimeError(f"{chain} exchange unavailable")
        if self.malformed_only:
            return [{"collectionAddress": "0x" + "4" * 40, "protocolData": {}}]
        return [
            {
                "offerId": f"offer-{chain}",
                "collectionAddress": "0x" + "4" * 40,
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.trace.append(f"cancel:{chain}")
        self.cancelled.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return self.cancel_result


def _install_broken_state(monkeypatch, trace: list[str]):
    class _BrokenPositionState:
        def __init__(self, db_path):
            trace.append(f"state_init:{db_path.name}")
            raise RuntimeError("execution DB init unavailable")

    monkeypatch.setattr(killswitch, "PositionState", _BrokenPositionState)


def test_state_constructor_failure_still_cancels_both_chains(monkeypatch, tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    _install_broken_state(monkeypatch, trace)
    api = _API()

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        api=api,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert trace.index("settings_dry:1") < trace.index("state_init:execution.sqlite3")
    assert api.cancelled == ["bsc", "eth"]
    assert result.preflight_error == "state_init: execution DB init unavailable"
    assert result.total_failed == 1
    assert [item.live_cancelled for item in result.chains] == [1, 1]
    assert all(item.failure_count == 0 for item in result.chains)
    assert all(item.fatal_error is None for item in result.chains)
    assert all(item.local_state_lookup_failed is False for item in result.chains)


def test_state_constructor_failure_plus_cancel_failure_preserves_both_failures(monkeypatch, tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    _install_broken_state(monkeypatch, trace)
    api = _API(cancel_result=False)

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        api=api,
        chains=("eth",),
    )

    assert api.cancelled == ["eth"]
    assert result.preflight_error == "state_init: execution DB init unavailable"
    assert result.chains[0].failed == ("offer-eth:cancel_failed",)
    assert result.chains[0].fatal_error is None
    assert result.total_failed == 2


def test_state_constructor_failure_plus_exchange_lookup_failure_is_fatal_chain(monkeypatch, tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    _install_broken_state(monkeypatch, trace)
    api = _API(fail_lookup=True)

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        api=api,
        chains=("eth",),
    )

    chain = result.chains[0]
    assert api.cancelled == []
    assert chain.exchange_lookup_failed is True
    assert chain.exchange_lookup_error == "eth exchange unavailable"
    assert chain.fatal_error == (
        "exchange lookup failed while local state unavailable: eth exchange unavailable"
    )
    assert chain.failure_count == 1
    assert result.preflight_error == "state_init: execution DB init unavailable"
    assert result.total_failed == 2


def test_state_constructor_failure_malformed_exchange_row_stays_visible_without_fake_cancel(monkeypatch, tmp_path):
    trace: list[str] = []
    settings = _Settings(trace, tmp_path / "execution.sqlite3")
    _install_broken_state(monkeypatch, trace)
    api = _API(malformed_only=True)

    result = killswitch.activate_multichain_killswitch(
        settings=settings,
        api=api,
        chains=("bsc",),
    )

    chain = result.chains[0]
    assert api.cancelled == []
    assert chain.exchange_seen == 1
    assert chain.live_cancelled == 0
    assert chain.failure_count == 1
    assert len(chain.failed) == 1
    assert chain.failed[0].startswith("exchange_unidentified_")
    assert chain.failed[0].endswith(":missing_order_id")
    assert result.preflight_error == "state_init: execution DB init unavailable"
    assert result.total_failed == 2
