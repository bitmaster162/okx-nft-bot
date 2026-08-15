from __future__ import annotations

from pathlib import Path

from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "7" * 40
CHAIN = "eth"
ORDER_ID = "order-r87"


def _store(tmp_path: Path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "execution-r87.sqlite3")


def test_repeated_mark_completed_is_idempotent_and_does_not_duplicate_audit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr87-complete",
    )

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
        actor="ops-r87-first",
        reason="external receipt independently confirmed",
    ) is True

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
        actor="ops-r87-repeat",
        reason="repeated operator action must be a no-op",
    ) is False

    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "completed"
    assert claims[0]["tx_hash"] == "0xr87-complete"

    rows = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "pending"
    assert rows[0]["prior_tx_hash"] == "0xr87-complete"
    assert rows[0]["resolution"] == "mark-completed"
    assert rows[0]["actor"] == "ops-r87-first"
