from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from okx_nft_bot.killswitch import activate_multichain_killswitch, format_killswitch_result


class _State:
    def __init__(self, trace: list[str], active=None) -> None:
        self.trace = trace
        self.active = active or {}
        self.submit_events: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []
        self.status_updates: list[tuple[str, str]] = []

    def audit_integrity(self):
        self.trace.append("audit")
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
        return list(self.active.get(chain, []))

    def mark_offer_status(self, *, order_hash: str, status: str):
        self.status_updates.append((order_hash, status))
        return True

    def record_submit_event(self, **kwargs):
        self.submit_events.append(dict(kwargs))

    def log_action(self, **kwargs):
        self.actions.append(dict(kwargs))


class _API:
    def __init__(self, trace: list[str], *, fail_lookup_chain: str | None = None) -> None:
        self.trace = trace
        self.fail_lookup_chain = fail_lookup_chain
        self.cancel_chains: list[str] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        self.trace.append(f"lookup:{chain}")
        if chain == self.fail_lookup_chain:
            raise RuntimeError(f"{chain} lookup unavailable")
        return [{"offerId": f"offer-{chain}", "protocolData": {"parameters": {"salt": "1"}}}]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.trace.append(f"cancel:{chain}")
        self.cancel_chains.append(chain)
        assert order_hash == f"offer-{chain}"
        assert order_params == {"salt": "1"}
        return True


def _settings(tmp_path: Path):
    return SimpleNamespace(execution_db_path=tmp_path / "execution.sqlite3")


def test_multichain_killswitch_disarms_before_network_and_cancels_all_chains(tmp_path: Path) -> None:
    trace: list[str] = []
    state = _State(trace)
    api = _API(trace)

    result = activate_multichain_killswitch(
        settings=_settings(tmp_path),
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert api.cancel_chains == ["bsc", "eth"]
    assert trace.index("disarm") < trace.index("lookup:bsc")
    assert trace.index("force_dry:1") < trace.index("lookup:bsc")
    assert [row["chain"] for row in state.submit_events] == ["bsc", "eth"]
    assert result.total_failed == 0
    text = format_killswitch_result(result)
    assert "chain=bsc" in text
    assert "chain=eth" in text
    assert "total_failed=0" in text


def test_lookup_failure_on_one_chain_does_not_skip_other_chain(tmp_path: Path) -> None:
    trace: list[str] = []
    state = _State(trace)
    api = _API(trace, fail_lookup_chain="bsc")

    result = activate_multichain_killswitch(
        settings=_settings(tmp_path),
        state=state,
        api=api,
        chains=("bsc", "eth"),
    )

    assert "lookup:bsc" in trace
    assert "lookup:eth" in trace
    assert api.cancel_chains == ["eth"]
    assert result.chains[0].exchange_lookup_failed is True
    assert result.chains[1].exchange_lookup_failed is False
    assert result.total_failed == 1


def test_failed_cancel_is_marked_killswitch_failed(tmp_path: Path) -> None:
    trace: list[str] = []
    offer = SimpleNamespace(order_hash="offer-eth")
    state = _State(trace, active={"eth": [offer]})

    class _FailingAPI(_API):
        def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
            self.trace.append(f"cancel:{chain}")
            return False

    result = activate_multichain_killswitch(
        settings=_settings(tmp_path),
        state=state,
        api=_FailingAPI(trace),
        chains=("eth",),
    )

    assert result.total_failed == 1
    assert ("offer-eth", "killswitch_failed") in state.status_updates
    assert state.actions[0]["executed"] is False
