from __future__ import annotations

import threading

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore
from okx_nft_bot.sniper.pending_effect_safety import install_pending_effect_safety


WALLET = "buyer-wallet-r90"
CHAIN = "eth"
ORDER_ID = "order-r90-success"
TX_HASH = "0xr90-success"


def _buyer_class(effect_calls: list[str]):
    class _Buyer:
        def __init__(self, db_path) -> None:
            self.execution_db_path = db_path
            self.buyer_address = WALLET
            self._failed_orders: set[str] = set()
            self._pending_orders: set[str] = set()
            self._lock = threading.Lock()

        def _execute_buy(
            self,
            listing: dict,
            chain: str,
            price: float,
            *,
            collection_address: str | None = None,
            currency: str | None = None,
        ) -> dict[str, object]:
            effect_calls.append(str(listing.get("orderId") or ""))
            return {"success": True, "tx_hash": TX_HASH, "gas_used": 12345}

    install_pending_effect_safety(_Buyer)
    return _Buyer


def test_confirmed_success_is_one_atomic_reserved_to_completed_transition(tmp_path) -> None:
    db_path = tmp_path / "execution-r90.sqlite3"
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls)

    result = Buyer(db_path)._execute_buy(
        {"orderId": ORDER_ID},
        CHAIN,
        0.01,
        collection_address="collection-r90",
        currency="WETH",
    )

    assert result["success"] is True
    assert effect_calls == [ORDER_ID]

    store = DurablePendingEffectStore(db_path)
    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "completed"
    assert claims[0]["tx_hash"] == TX_HASH

    resolutions = store.fetch_resolutions(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
    )
    assert len(resolutions) == 1
    assert resolutions[0]["prior_state"] == "reserved"
    assert resolutions[0]["prior_tx_hash"] is None
    assert resolutions[0]["actor"] == "instant-buyer-runtime"
    assert TX_HASH in resolutions[0]["reason"]


def test_unproven_success_finalization_retains_in_memory_and_durable_claim(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "execution-r90-failclosed.sqlite3"
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls)

    monkeypatch.setattr(
        DurablePendingEffectStore,
        "resolve_claim",
        lambda self, **kwargs: False,
    )

    buyer = Buyer(db_path)
    result = buyer._execute_buy(
        {"orderId": ORDER_ID},
        CHAIN,
        0.01,
        collection_address="collection-r90",
        currency="WETH",
    )

    assert result["success"] is True
    assert effect_calls == [ORDER_ID]
    assert ORDER_ID in buyer._pending_orders

    store = DurablePendingEffectStore(db_path)
    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "reserved"

    replay_calls: list[str] = []
    ReplayBuyer = _buyer_class(replay_calls)
    replay = ReplayBuyer(db_path)._execute_buy(
        {"orderId": ORDER_ID},
        CHAIN,
        0.01,
        collection_address="collection-r90",
        currency="WETH",
    )
    assert replay["pending"] is True
    assert replay_calls == []
