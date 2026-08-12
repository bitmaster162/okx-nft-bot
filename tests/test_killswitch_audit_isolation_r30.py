from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from okx_nft_bot.killswitch import activate_multichain_killswitch


class _AuditFailState:
    def __init__(self, *, fail_active_chain: str | None = None):
        self.fail_active_chain = fail_active_chain
        self.trace: list[str] = []
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []

    def audit_integrity(self):
        self.trace.append("integrity")
        return SimpleNamespace(ok=True)

    def disarm_live(self, **_kwargs):
        self.trace.append("disarm")
        return {"armed": False}

    def set_force_dry_run(self, value: bool, **_kwargs):
        self.trace.append(f"force_dry:{int(value)}")

    def set_runtime_value(self, key: str, _value: object):
        self.trace.append(f"runtime:{key}")

    def get_active_offers(self, *, chain: str):
        self.trace.append(f"active:{chain}")
        if chain == self.fail_active_chain:
            raise RuntimeError(f"{chain} state unavailable")
        return []

    def upsert_active_offer(self, **_kwargs):
        raise AssertionError("successful cancel path must not upsert a zombie")

    def mark_offer_status(self, **_kwargs):
        return False

    def record_submit_event(self, **kwargs):
        self.trace.append(f"audit_submit:{kwargs['chain']}")
        if kwargs["chain"] == "bsc":
            raise RuntimeError("bsc audit persistence unavailable")
        self.submit_events.append(dict(kwargs))

    def log_action(self, **kwargs):
        self.trace.append(f"audit_action:{kwargs['chain']}")
        self.actions.append(dict(kwargs))


class _API:
    def __init__(self):
        self.trace: list[str] = []
        self.cancel_chains: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        return [
            {
                "offerId": f"offer-{chain}",
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.trace.append(f"cancel:{chain}")
        self.cancel_chains.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return True


def _settings(tmp_path: Path):
    return SimpleNamespace(execution_db_path=tmp_path / "execution.sqlite3")


def test_post_cancel_audit_failure_does_not_skip_next_chain(tmp_path):
    state = _AuditFailState()
    api = _API()

    result = activate_multichain_killswitch(
        settings=_settings(tmp_path),
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert api.cancel_chains == ["bsc", "eth"]
    assert len(result.chains) == 2
    assert result.chains[0].chain == "bsc"
    assert result.chains[0].fatal_error == "bsc audit persistence unavailable"
    assert result.chains[1].chain == "eth"
    assert result.chains[1].fatal_error is None
    assert result.chains[1].live_cancelled == 1
    assert result.total_failed == 1
    assert [row["chain"] for row in state.submit_events] == ["eth"]
    assert "audit_submit:bsc" in state.trace
    assert "audit_submit:eth" in state.trace


def test_local_state_read_failure_plus_audit_failure_still_cancels_both_chains(tmp_path):
    state = _AuditFailState(fail_active_chain="bsc")
    api = _API()

    result = activate_multichain_killswitch(
        settings=_settings(tmp_path),
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert "lookup:bsc" in api.trace
    assert "lookup:eth" in api.trace
    assert api.cancel_chains == ["bsc", "eth"]
    assert len(result.chains) == 2
    assert result.chains[0].fatal_error is None
    assert result.chains[0].local_state_lookup_failed is True
    assert result.chains[0].local_state_lookup_error == "bsc state unavailable"
    assert result.chains[0].live_cancelled == 1
    assert result.chains[1].fatal_error is None
    assert result.chains[1].live_cancelled == 1
    assert result.total_failed == 1
