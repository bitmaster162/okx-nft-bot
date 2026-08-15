from __future__ import annotations

import sqlite3
import threading

import pytest

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


def test_confirmed_success_cannot_be_left_releaseable_if_second_phase_fails(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "execution-r90-split.sqlite3"
    effect_calls: list[str] = []
    Buyer = _buyer_class(effect_calls)

    def _fail_second_phase(self, **kwargs):
        raise RuntimeError("simulated completion audit failure")

    monkeypatch.setattr(DurablePendingEffectStore, "resolve_claim", _fail_second_phase)

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
    # A receipt-confirmed success must never degrade into the ordinary pending
    # reconciliation lane, because pending is intentionally releaseable for retry.
    assert claims[0]["state"] != "pending"


def test_atomic_success_finalization_rolls_back_state_and_tx_hash_if_audit_fails(
    tmp_path,
) -> None:
    db_path = tmp_path / "execution-r90-atomic.sqlite3"
    store = DurablePendingEffectStore(db_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER abort_r90_runtime_completion_audit
            BEFORE INSERT ON instant_buy_claim_resolutions
            WHEN NEW.actor='instant-buyer-runtime-r90'
            BEGIN
                SELECT RAISE(ABORT, 'simulated r90 audit insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated r90 audit insert failure"):
        store.complete_success(
            wallet=WALLET,
            chain=CHAIN,
            order_id=ORDER_ID,
            tx_hash=TX_HASH,
            actor="instant-buyer-runtime-r90",
            reason="receipt-confirmed successful instant-buy",
        )

    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "reserved"
    assert claims[0]["tx_hash"] is None
    assert store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []
