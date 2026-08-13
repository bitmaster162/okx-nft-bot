from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.undercutter.state import PositionState


class _SnapshotAPI:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, bool]] = []

    def get_my_offers(self, *, chain: str, require_all_endpoints: bool):
        self.calls.append((chain, require_all_endpoints))
        return list(self.rows)


def _governor(tmp_path, rows: list[dict[str, object]]):
    state = PositionState(tmp_path / "execution.sqlite3")
    api = _SnapshotAPI(rows)
    governor = ExecutionGovernor.__new__(ExecutionGovernor)
    governor.settings = SimpleNamespace(execution_chain="bsc")
    governor.state = state
    governor.api_client = api
    return governor, state, api


def _add_local(
    state: PositionState,
    *,
    order_hash: str,
    collection: str = "0xcollection",
    price_bnb: float = 0.1,
) -> None:
    state.upsert_active_offer(
        order_hash=order_hash,
        collection=collection,
        chain="bsc",
        price_bnb=price_bnb,
        status="active",
        current_floor=price_bnb,
    )


def test_malformed_snapshot_preserves_unmatched_local_live_offer(tmp_path) -> None:
    governor, state, api = _governor(
        tmp_path,
        [{"price": "100000000000000000"}],
    )
    _add_local(state, order_hash="local-live")

    result = governor.reconcile_active_offers(chain="bsc")

    assert api.calls == [("bsc", True)]
    assert result.malformed_exchange_rows == 1
    assert result.local_marked_missing == 0
    assert result.exchange_missing_order_hashes == []
    assert [offer.order_hash for offer in state.get_active_offers(chain="bsc")] == ["local-live"]
    runtime = state.get_runtime_state()
    assert str(runtime.get("last_reconcile_malformed_exchange_rows")) == "1"


def test_clean_empty_snapshot_still_marks_local_missing(tmp_path) -> None:
    governor, state, api = _governor(tmp_path, [])
    _add_local(state, order_hash="local-missing")

    result = governor.reconcile_active_offers(chain="bsc")

    assert api.calls == [("bsc", True)]
    assert result.malformed_exchange_rows == 0
    assert result.local_marked_missing == 1
    assert result.exchange_missing_order_hashes == ["local-missing"]
    assert state.get_active_offers(chain="bsc") == []


def test_malformed_snapshot_still_refreshes_positive_match(tmp_path) -> None:
    governor, state, _api = _governor(
        tmp_path,
        [
            {},
            {
                "orderHash": "local-match",
                "collectionAddress": "0xupdated",
                "price": "200000000000000000",
            },
        ],
    )
    _add_local(
        state,
        order_hash="local-match",
        collection="0xold",
        price_bnb=0.1,
    )

    result = governor.reconcile_active_offers(chain="bsc")
    active = state.get_active_offers(chain="bsc")

    assert result.malformed_exchange_rows == 1
    assert result.local_marked_missing == 0
    assert result.local_refreshed == 1
    assert len(active) == 1
    assert active[0].order_hash == "local-match"
    assert active[0].collection == "0xupdated"
    assert active[0].price_bnb == 0.2


def test_malformed_snapshot_still_imports_well_formed_exchange_offer(tmp_path) -> None:
    governor, state, api = _governor(
        tmp_path,
        [
            {"price": "100000000000000000"},
            {
                "offerId": "exchange-new",
                "collectionAddress": "0xnew",
                "price": "300000000000000000",
            },
        ],
    )
    _add_local(state, order_hash="local-live")

    result = governor.reconcile_active_offers(chain="bsc")
    active = {offer.order_hash: offer for offer in state.get_active_offers(chain="bsc")}

    assert api.calls == [("bsc", True)]
    assert result.malformed_exchange_rows == 1
    assert result.local_marked_missing == 0
    assert result.local_added_from_exchange == 1
    assert result.imported_order_hashes == ["exchange-new"]
    assert set(active) == {"local-live", "exchange-new"}
    assert active["exchange-new"].collection == "0xnew"
    assert active["exchange-new"].price_bnb == 0.3
