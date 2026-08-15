from __future__ import annotations

import threading

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore
from okx_nft_bot.sniper.pending_effect_safety import install_pending_effect_safety


WALLET = "buyer-wallet-r90"
CHAIN = "eth"
ORDER_ID = "order-r90-success"
TX_HASH = "0xr90-success"


def test_confirmed_success_cannot_be_released_between_tx_hash_and_completion(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "execution-r90.sqlite3"
    listing = {"orderId": ORDER_ID}
    effect_calls: list[str] = []
    release_results: list[bool] = []

    class Buyer:
        def __init__(self) -> None:
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

    original_mark_pending = DurablePendingEffectStore.mark_pending

    def mark_pending_then_release(
        self: DurablePendingEffectStore,
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
        release_results.append(
            DurablePendingEffectStore(db_path).resolve_claim(
                wallet=wallet,
                chain=chain,
                order_id=order_id,
                resolution="release-for-retry",
                actor="operator-race",
                reason="simulated stale retry release between R89 finalization transactions",
            )
        )

    monkeypatch.setattr(DurablePendingEffectStore, "mark_pending", mark_pending_then_release)
    install_pending_effect_safety(Buyer)

    result = Buyer()._execute_buy(
        listing,
        CHAIN,
        0.01,
        collection_address="collection-r90",
        currency="WETH",
    )

    assert result["success"] is True
    assert effect_calls == [ORDER_ID]
    # Old R89 code enters the hook and the simulated release succeeds. R90 may
    # eliminate that pre-completion mark_pending call entirely; either way the
    # terminal receipt-confirmed tombstone below is the safety invariant.
    assert release_results in ([], [True])

    store = DurablePendingEffectStore(db_path)
    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "completed"
    assert claims[0]["tx_hash"] == TX_HASH

    resolutions = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    runtime_completions = [
        row
        for row in resolutions
        if row["resolution"] == "mark-completed" and row["actor"] == "instant-buyer-runtime"
    ]
    assert len(runtime_completions) == 1
