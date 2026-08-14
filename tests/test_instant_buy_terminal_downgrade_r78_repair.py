from __future__ import annotations

from pathlib import Path

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "8" * 40
CHAIN = "eth"
ORDER_ID = "order-r78"


def test_stale_mark_pending_cannot_downgrade_completed_claim(tmp_path: Path) -> None:
    store = DurablePendingEffectStore(tmp_path / "execution-r78.sqlite3")

    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xconfirmed-r78",
    )
    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
    ) is True

    # A stale process can return from the effect path after the operator has
    # already reconciled the claim as completed. Its late enrichment must not
    # reopen the terminal tombstone or replace the confirmed receipt.
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xstale-r78",
    )

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert rows[0]["tx_hash"] == "0xconfirmed-r78"
    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
    ) is False
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is False
