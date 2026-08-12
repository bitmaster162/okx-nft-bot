from __future__ import annotations

from okx_nft_bot.killswitch import _cancel_chain
from okx_nft_bot.undercutter.state import PositionState


COLLECTION = "0x" + "3" * 40


class _API:
    def __init__(self, *, rows=None, lookup_error=None):
        self.rows = list(rows or [])
        self.lookup_error = lookup_error
        self.cancel_calls = []

    def get_my_offers(self, *, chain, require_all_endpoints):
        assert chain == "eth"
        assert require_all_endpoints is True
        if self.lookup_error is not None:
            raise self.lookup_error
        return list(self.rows)

    def cancel_offer(self, order_hash, *, chain, order_params=None):
        self.cancel_calls.append(
            {
                "order_hash": order_hash,
                "chain": chain,
                "order_params": order_params,
            }
        )
        return True


def _state(tmp_path):
    return PositionState(tmp_path / "execution.sqlite3")


def _add_opensea(state, *, order_hash="os-order-1"):
    state.upsert_active_offer(
        order_hash=order_hash,
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.25,
        status="active",
        preview_payload={
            "marketplace": "opensea",
            "source": "counter_bidder_mirror",
        },
    )


def test_opensea_inventory_is_never_sent_to_okx_cancel(tmp_path):
    state = _state(tmp_path)
    _add_opensea(state)
    api = _API(rows=[])

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert api.cancel_calls == []
    assert result.active_offers_seen == 1
    assert result.exchange_seen == 0
    assert result.live_cancelled == 0
    assert result.already_gone == 0
    assert result.failed == ("os-order-1:opensea_cancel_unavailable",)
    assert result.failure_count == 1

    assert state.get_active_offers(chain="eth") == []
    failed = state.get_killswitch_failed_offers(chain="eth")
    assert [offer.order_hash for offer in failed] == ["os-order-1"]
    assert failed[0].preview_payload["marketplace"] == "opensea"


def test_okx_lookup_failure_fallback_filters_out_opensea_inventory(tmp_path):
    state = _state(tmp_path)
    state.upsert_active_offer(
        order_hash="okx-order-1",
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.1,
        status="active",
    )
    _add_opensea(state)
    api = _API(lookup_error=RuntimeError("inventory unavailable"))

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert [call["order_hash"] for call in api.cancel_calls] == ["okx-order-1"]
    assert result.exchange_lookup_failed is True
    assert result.live_cancelled == 1
    assert "os-order-1:opensea_cancel_unavailable" in result.failed

    assert state.get_active_offers(chain="eth") == []
    failed = state.get_killswitch_failed_offers(chain="eth")
    assert [offer.order_hash for offer in failed] == ["os-order-1"]


def test_legacy_untagged_inventory_remains_okx_compatible(tmp_path):
    state = _state(tmp_path)
    state.upsert_active_offer(
        order_hash="okx-order-legacy",
        collection=COLLECTION,
        chain="eth",
        price_bnb=0.1,
        status="active",
    )
    api = _API(
        rows=[
            {
                "offerId": "okx-order-legacy",
                "collectionAddress": COLLECTION,
            }
        ]
    )

    result = _cancel_chain(state=state, api=api, chain="eth")

    assert [call["order_hash"] for call in api.cancel_calls] == ["okx-order-legacy"]
    assert result.live_cancelled == 1
    assert result.failed == ()
    assert result.failure_count == 0
    assert state.get_active_offers(chain="eth") == []
    assert state.get_killswitch_failed_offers(chain="eth") == []
