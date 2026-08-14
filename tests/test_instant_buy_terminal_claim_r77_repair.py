from __future__ import annotations

from pathlib import Path

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "7" * 40
CHAIN = "eth"
ORDER_ID = "order-r77"


def _store(tmp_path: Path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "execution-r77.sqlite3")


def _mark_completed(store: DurablePendingEffectStore) -> None:
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr77",
    )
    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
    ) is True


def test_release_for_retry_cannot_delete_completed_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _mark_completed(store)

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
    ) is False

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert rows[0]["tx_hash"] == "0xr77"
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is False


def test_low_level_release_cannot_delete_completed_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _mark_completed(store)

    store.release(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is False
