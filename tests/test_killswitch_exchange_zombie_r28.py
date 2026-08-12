from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.killswitch import _cancel_chain
from okx_nft_bot.undercutter.state import PositionState


COLLECTION = "0x" + "3" * 40


class _ExchangeOnlyAPI:
    def __init__(self, *, cancel_result: bool | None = False, raise_cancel: bool = False):
        self.cancel_result = cancel_result
        self.raise_cancel = raise_cancel
        self.cancel_calls = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        return [
            {
                "offerId": "exchange-only-zombie",
                "contractAddress": COLLECTION,
                "protocolData": {"parameters": {"salt": "1"}},
            }
        ]

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancel_calls.append((order_hash, chain, order_params))
        if self.raise_cancel:
            raise RuntimeError("cancel transport unavailable")
        return bool(self.cancel_result)


def _settings(db_path):
    return SimpleNamespace(
        execution_db_path=db_path,
        dry_run=False,
        max_live_offers_per_hour=10,
        max_bnb_per_day=5.0,
        submit_cooldown_seconds=0,
    )


def test_exchange_only_failed_cancel_persists_killswitch_failed_and_governor_veto(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    api = _ExchangeOnlyAPI(cancel_result=False)

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert result.failure_count == 1
    assert result.exchange_seen == 1
    assert result.live_cancelled == 0
    zombies = state.get_killswitch_failed_offers(chain="eth")
    assert len(zombies) == 1
    assert zombies[0].order_hash == "exchange-only-zombie"
    assert zombies[0].collection == COLLECTION
    assert zombies[0].price_bnb == 0.0
    assert state.get_active_offers(chain="eth") == []

    governor = ExecutionGovernor(
        settings=_settings(db_path),
        state=state,
        api_client=api,
    )
    blocked = governor.check_live_submit_allowed(
        action_type="LIVE_TEST",
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.1,
    )
    assert blocked is not None
    assert blocked.startswith("killswitch_failed: 1 zombie offer(s)")


def test_exchange_only_cancel_exception_also_persists_zombie(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    api = _ExchangeOnlyAPI(raise_cancel=True)

    result = _cancel_chain(state=state, api=api, chain="bsc")

    assert result.failure_count == 1
    assert "cancel transport unavailable" in result.failed[0]
    zombies = state.get_killswitch_failed_offers(chain="bsc")
    assert [row.order_hash for row in zombies] == ["exchange-only-zombie"]
    assert zombies[0].collection == COLLECTION


def test_successful_exchange_only_cancel_does_not_create_local_zombie(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    api = _ExchangeOnlyAPI(cancel_result=True)

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert result.failure_count == 0
    assert result.live_cancelled == 1
    assert state.get_killswitch_failed_offers(chain="eth") == []
    assert state.get_active_offers(chain="eth") == []


def test_exchange_only_failed_cancel_without_collection_uses_explicit_sentinel(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)

    class _NoCollectionAPI(_ExchangeOnlyAPI):
        def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
            assert require_all_endpoints is True
            return [{"offerId": "exchange-only-zombie", "protocolData": {}}]

    result = _cancel_chain(
        state=state,
        api=_NoCollectionAPI(cancel_result=False),
        chain="eth",
    )

    assert result.failure_count == 1
    zombies = state.get_killswitch_failed_offers(chain="eth")
    assert len(zombies) == 1
    assert zombies[0].collection == "exchange_unknown"
    assert zombies[0].price_bnb == 0.0
