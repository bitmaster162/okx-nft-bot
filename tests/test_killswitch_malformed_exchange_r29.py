from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.killswitch import _cancel_chain, _unidentified_exchange_id
from okx_nft_bot.undercutter.state import PositionState


COLLECTION = "0x" + "4" * 40


def _settings(db_path):
    return SimpleNamespace(
        execution_db_path=db_path,
        dry_run=False,
        max_live_offers_per_hour=10,
        max_bnb_per_day=5.0,
        submit_cooldown_seconds=0,
    )


class _MalformedAPI:
    def __init__(self, rows):
        self.rows = rows
        self.cancel_calls = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        assert require_all_endpoints is True
        return list(self.rows)

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancel_calls.append((order_hash, chain, order_params))
        return True


def test_missing_order_id_creates_quarantine_and_governor_veto(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    row = {
        "contractAddress": COLLECTION,
        "protocolData": {"parameters": {"salt": "1"}},
    }
    api = _MalformedAPI([row])

    result = _cancel_chain(state=state, api=api, chain="eth")

    expected_id = _unidentified_exchange_id(row)
    assert result.exchange_seen == 1
    assert result.live_cancelled == 0
    assert result.failure_count == 1
    assert result.failed == (f"{expected_id}:missing_order_id",)
    assert api.cancel_calls == []

    zombies = state.get_killswitch_failed_offers(chain="eth")
    assert len(zombies) == 1
    assert zombies[0].order_hash == expected_id
    assert zombies[0].collection == COLLECTION
    assert zombies[0].price_bnb == 0.0

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


def test_duplicate_malformed_rows_dedupe_to_one_quarantine(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    row = {"collectionAddress": COLLECTION, "protocolData": {}}
    api = _MalformedAPI([row, dict(row), dict(row)])

    result = _cancel_chain(state=state, api=api, chain="bsc")

    assert result.exchange_seen == 1
    assert result.failure_count == 1
    zombies = state.get_killswitch_failed_offers(chain="bsc")
    assert len(zombies) == 1
    assert zombies[0].order_hash.startswith("exchange_unidentified_")
    assert api.cancel_calls == []


def test_malformed_and_identified_rows_are_reported_independently(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    malformed = {"contractAddress": COLLECTION, "protocolData": {}}
    identified = {
        "offerId": "known-offer",
        "contractAddress": COLLECTION,
        "protocolData": {"parameters": {"salt": "2"}},
    }
    api = _MalformedAPI([malformed, identified])

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert result.exchange_seen == 2
    assert result.live_cancelled == 1
    assert result.failure_count == 1
    assert api.cancel_calls == [("known-offer", "eth", {"salt": "2"})]
    zombies = state.get_killswitch_failed_offers(chain="eth")
    assert len(zombies) == 1
    assert zombies[0].order_hash.startswith("exchange_unidentified_")


def test_unidentified_fingerprint_is_stable_across_dict_key_order():
    first = {
        "contractAddress": COLLECTION,
        "protocolData": {"parameters": {"salt": "1", "counter": "7"}},
    }
    second = {
        "protocolData": {"parameters": {"counter": "7", "salt": "1"}},
        "contractAddress": COLLECTION,
    }

    assert _unidentified_exchange_id(first) == _unidentified_exchange_id(second)
