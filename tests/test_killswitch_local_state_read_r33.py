from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from okx_nft_bot.killswitch import _cancel_chain, activate_multichain_killswitch, format_killswitch_result


class _BrokenReadState:
    def __init__(self, *, audit_write_fails: bool = True) -> None:
        self.audit_write_fails = audit_write_fails
        self.trace: list[str] = []

    def get_active_offers(self, *, chain: str):
        self.trace.append(f"active:{chain}")
        raise RuntimeError(f"{chain} sqlite read unavailable")

    def mark_offer_status(self, **_kwargs):
        raise AssertionError("no local rows were readable")

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("successful exchange cancel must not create zombie state")

    def record_submit_event(self, **kwargs):
        self.trace.append(f"audit_submit:{kwargs['chain']}")
        if self.audit_write_fails:
            raise RuntimeError("audit sqlite unavailable")
        return 1

    def log_action(self, **kwargs):
        self.trace.append(f"audit_action:{kwargs['chain']}")
        if self.audit_write_fails:
            raise RuntimeError("audit sqlite unavailable")
        return 1

    def disarm_live(self, **_kwargs):
        self.trace.append("disarm")
        return {"armed": False}

    def set_force_dry_run(self, value: bool, **_kwargs):
        self.trace.append(f"force_dry:{int(value)}")

    def set_runtime_value(self, key: str, _value: object):
        self.trace.append(f"runtime:{key}")

    def audit_integrity(self):
        self.trace.append("integrity")
        return None


class _API:
    def __init__(self, *, fail_lookup: bool = False) -> None:
        self.fail_lookup = fail_lookup
        self.trace: list[str] = []
        self.cancelled: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        if self.fail_lookup:
            raise RuntimeError(f"{chain} exchange unavailable")
        return [
            {
                "offerId": f"offer-{chain}",
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.trace.append(f"cancel:{chain}")
        self.cancelled.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return True


def test_local_state_read_and_audit_failures_do_not_erase_successful_exchange_cancel():
    state = _BrokenReadState(audit_write_fails=True)
    api = _API()

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert api.cancelled == ["eth"]
    assert result.live_cancelled == 1
    assert result.exchange_seen == 1
    assert result.active_offers_seen == 0
    assert result.local_state_lookup_failed is True
    assert result.local_state_lookup_error == "eth sqlite read unavailable"
    assert result.exchange_lookup_failed is False
    assert result.fatal_error is None
    assert result.failure_count == 1
    assert "audit_submit:eth" in state.trace


def test_multichain_exchange_cancel_continues_when_local_state_reads_fail(tmp_path):
    state = _BrokenReadState(audit_write_fails=True)
    api = _API()
    settings = SimpleNamespace(
        execution_db_path=tmp_path / "execution.sqlite3",
        dry_run=False,
    )

    result = activate_multichain_killswitch(
        settings=settings,
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert settings.dry_run is True
    assert api.cancelled == ["bsc", "eth"]
    assert [item.live_cancelled for item in result.chains] == [1, 1]
    assert all(item.local_state_lookup_failed for item in result.chains)
    assert all(item.fatal_error is None for item in result.chains)
    assert result.total_failed == 2

    text = format_killswitch_result(result)
    assert "chain=bsc" in text and "state_lookup_failed=1" in text
    assert "chain=eth" in text
    assert "total_failed=2" in text


def test_local_and_exchange_discovery_failure_is_not_reported_clean():
    state = _BrokenReadState(audit_write_fails=True)
    api = _API(fail_lookup=True)

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert api.cancelled == []
    assert result.live_cancelled == 0
    assert result.exchange_seen == 0
    assert result.local_state_lookup_failed is True
    assert result.exchange_lookup_failed is True
    assert result.exchange_lookup_error == "eth exchange unavailable"
    assert result.failure_count == 2

    text = format_killswitch_result(
        SimpleNamespace(
            activated_at="2026-08-12T00:00:00+00:00",
            chains=(result,),
            preflight_error=None,
            total_failed=result.failure_count,
        )
    )
    assert "state_lookup_failed=1" in text
    assert "exchange_lookup_failed=1" in text
    assert "total_failed=2" in text
