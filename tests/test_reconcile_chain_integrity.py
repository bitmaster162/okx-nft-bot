from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.undercutter.state import PositionState


class _EmptyAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        self.calls.append((chain, require_all_endpoints))
        return []


def _governor(tmp_path, chain: str) -> tuple[ExecutionGovernor, PositionState, _EmptyAPI]:
    state = PositionState(tmp_path / "execution.sqlite3")
    api = _EmptyAPI()
    governor = ExecutionGovernor.__new__(ExecutionGovernor)
    governor.settings = SimpleNamespace(execution_chain=chain)
    governor.state = state
    governor.api_client = api
    return governor, state, api


@pytest.mark.parametrize("chain", ["bsc", "eth"])
def test_reconcile_keeps_multichain_runtime_integrity_clean(tmp_path, chain):
    governor, state, api = _governor(tmp_path, chain)

    result = governor.reconcile_active_offers(chain=chain)
    runtime = state.get_runtime_state()

    assert result.chain == chain
    assert api.calls == [(chain, True)]
    assert runtime.get("last_reconcile_at")
    assert runtime.get(f"last_reconcile_at_{chain}")
    assert "last_reconcile_chain" not in runtime

    audit = state.audit_integrity()
    runtime_after_audit = state.get_runtime_state()

    assert audit.ok is True
    assert audit.issue_count == 0
    assert audit.quarantine_count == 0
    assert "last_reconcile_chain" not in audit.runtime_keys_cleared
    assert "last_reconcile_chain" not in runtime_after_audit
    assert runtime_after_audit.get(f"last_reconcile_at_{chain}")


def test_reconcile_rejects_unsupported_chain_before_api_call(tmp_path):
    governor, _state, api = _governor(tmp_path, "bsc")

    with pytest.raises(ValueError, match="not in supported"):
        governor.reconcile_active_offers(chain="polygon")

    assert api.calls == []
