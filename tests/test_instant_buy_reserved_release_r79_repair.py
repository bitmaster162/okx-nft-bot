from __future__ import annotations

from pathlib import Path

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "9" * 40
CHAIN = "eth"
ORDER_ID = "order-r79"


def _store(tmp_path: Path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "execution-r79.sqlite3")


def test_reserved_claim_cannot_be_released_for_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
    ) is False

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["state"] == "reserved"
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is False


def test_pending_claim_can_still_be_released_for_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr79",
    )

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
    ) is True
    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
