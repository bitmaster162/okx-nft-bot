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


def test_confirmed_success_cannot_be_released_between_tx_capture_and_completion(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "execution-r90.sqlite3"
    listing = {"orderId": ORDER_ID}
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls)

    original_mark_pending = DurablePendingEffectStore.mark_pending
    raced_release: list[bool] = []

    def racing_mark_pending(
        self,
        *,
        wallet: str,
        chain: str,
        order_id: str,
        tx_hash: str | None = None,
    ) -> None:
        original_mark_pending(
            self,
            wallet=wallet,
            chain=chain,
            order_id=order_id,
            tx_hash=tx_hash,
        )
        rival = DurablePendingEffectStore(self.db_path)
        raced_release.append(
            rival.resolve_claim(
                wallet=wallet,
                chain=chain,
                order_id=order_id,
                resolution="release-for-retry",
                actor="r90-racing-operator",
                reason="simulate operator release in the old two-transaction success gap",
            )
        )

    monkeypatch.setattr(DurablePendingEffectStore, "mark_pending", racing_mark_pending)

    result = Buyer(db_path)._execute_buy(
        listing,
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

    resolutions = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert [row["resolution"] for row in resolutions] == ["mark-completed"]
    assert resolutions[0]["actor"] == "instant-buyer-runtime"

    # Under the repaired atomic path the old mark_pending hook is never exposed,
    # so a competing release cannot land between tx capture and terminalization.
    assert raced_release == []
