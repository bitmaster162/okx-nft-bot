from __future__ import annotations

import threading

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore
from okx_nft_bot.sniper.pending_effect_safety import install_pending_effect_safety


WALLET = "buyer-wallet-r89"
CHAIN = "eth"
ORDER_ID = "order-r89-success"
TX_HASH = "0xr89-success"


def _buyer_class(effect_calls: list[str], *, success: bool = True):
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
            if success:
                return {"success": True, "tx_hash": TX_HASH, "gas_used": 12345}
            return {"success": False, "error": "DETERMINISTIC_NO_EFFECT"}

    install_pending_effect_safety(_Buyer)
    return _Buyer


def test_confirmed_success_keeps_terminal_tombstone_and_blocks_restart(tmp_path) -> None:
    db_path = tmp_path / "execution-r89.sqlite3"
    listing = {"orderId": ORDER_ID}
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls)

    first_buyer = Buyer(db_path)
    first = first_buyer._execute_buy(
        listing,
        CHAIN,
        0.01,
        collection_address="collection-r89",
        currency="WETH",
    )

    assert first["success"] is True
    assert effect_calls == [ORDER_ID]

    store = DurablePendingEffectStore(db_path)
    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "completed"
    assert claims[0]["tx_hash"] == TX_HASH

    restarted_buyer = Buyer(db_path)
    second = restarted_buyer._execute_buy(
        listing,
        CHAIN,
        0.01,
        collection_address="collection-r89",
        currency="WETH",
    )

    assert effect_calls == [ORDER_ID]
    assert second["success"] is False
    assert second.get("pending") is True
    assert "pending" in str(second.get("error", "")).lower()

    claims_after_restart = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims_after_restart) == 1
    assert claims_after_restart[0]["state"] == "completed"
    assert claims_after_restart[0]["tx_hash"] == TX_HASH


def test_deterministic_no_effect_failure_remains_retryable(tmp_path) -> None:
    db_path = tmp_path / "execution-r89-no-effect.sqlite3"
    listing = {"orderId": "order-r89-no-effect"}
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls, success=False)

    first = Buyer(db_path)._execute_buy(
        listing,
        CHAIN,
        0.01,
        collection_address="collection-r89",
        currency="WETH",
    )
    second = Buyer(db_path)._execute_buy(
        listing,
        CHAIN,
        0.01,
        collection_address="collection-r89",
        currency="WETH",
    )

    assert first["success"] is False
    assert second["success"] is False
    assert effect_calls == ["order-r89-no-effect", "order-r89-no-effect"]
    store = DurablePendingEffectStore(db_path)
    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id="order-r89-no-effect") == []
