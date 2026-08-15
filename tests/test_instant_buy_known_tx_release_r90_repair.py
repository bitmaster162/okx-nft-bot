from __future__ import annotations

from okx_nft_bot import cli_entry
from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "9" * 40
CHAIN = "eth"
ORDER_ID = "order-r90"
TX_HASH = "0xr90-known"
ACTOR = "ops-r90"
REASON = "independently confirmed transaction produced no external effect"


def _store(tmp_path):
    return DurablePendingEffectStore(tmp_path / "execution-r90.sqlite3")


def test_pending_without_tx_hash_remains_releaseable(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason="confirmed no external effect before any transaction hash existed",
    ) is True
    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []


def test_known_tx_hash_blocks_ordinary_release_and_preserves_claim_and_audit(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, tx_hash=TX_HASH)

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason="ordinary retry request must not erase a known transaction",
    ) is False

    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "pending"
    assert claims[0]["tx_hash"] == TX_HASH
    assert store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []


def test_known_tx_hash_requires_explicit_no_effect_attestation_and_records_special_resolution(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, tx_hash=TX_HASH)

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason=REASON,
        known_tx_no_effect_confirmed=True,
    ) is True

    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []
    rows = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "pending"
    assert rows[0]["prior_tx_hash"] == TX_HASH
    assert rows[0]["resolution"] == "release-for-retry-known-tx-no-effect"
    assert rows[0]["actor"] == ACTOR
    assert rows[0]["reason"] == REASON


def test_cli_exposes_known_tx_no_effect_confirmation_flag() -> None:
    parser = cli_entry.build_parser()
    args = parser.parse_args([
        "resolve-instant-buy-claim",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
        "--resolution",
        "release-for-retry",
        "--tx-no-effect-confirmed",
        "--actor",
        ACTOR,
        "--reason",
        REASON,
        "--yes",
    ])
    assert args.tx_no_effect_confirmed is True
