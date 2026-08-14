from __future__ import annotations

from pathlib import Path

import pytest

from okx_nft_bot import cli_entry
from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "8" * 40
CHAIN = "eth"
ORDER_ID = "order-r80"
ACTOR = "ops-r80"
REASON = "confirmed no external effect before retry"


def _store(tmp_path: Path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "execution-r80.sqlite3")


def test_release_for_retry_keeps_durable_audit_after_claim_is_removed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr80-pending",
    )

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason=REASON,
    ) is True

    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []
    rows = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "pending"
    assert rows[0]["prior_tx_hash"] == "0xr80-pending"
    assert rows[0]["resolution"] == "release-for-retry"
    assert rows[0]["actor"] == ACTOR
    assert rows[0]["reason"] == REASON

    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    assert len(store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)) == 1


def test_mark_completed_records_resolution_and_preserves_terminal_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, tx_hash="0xr80-complete")

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
        actor=ACTOR,
        reason="external receipt independently confirmed",
    ) is True

    claims = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(claims) == 1
    assert claims[0]["state"] == "completed"
    rows = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "pending"
    assert rows[0]["prior_tx_hash"] == "0xr80-complete"
    assert rows[0]["resolution"] == "mark-completed"


def test_failed_resolution_does_not_invent_audit_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason=REASON,
    ) is False
    assert store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []


def test_cli_requires_explicit_actor_and_reason_for_mutating_resolution() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([
            "resolve-instant-buy-claim",
            "--wallet",
            WALLET,
            "--chain",
            CHAIN,
            "--order-id",
            ORDER_ID,
            "--resolution",
            "mark-completed",
            "--yes",
        ])

    args = parser.parse_args([
        "resolve-instant-buy-claim",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
        "--resolution",
        "mark-completed",
        "--actor",
        ACTOR,
        "--reason",
        "receipt confirmed by operator",
        "--yes",
    ])
    assert args.actor == ACTOR
    assert args.reason == "receipt confirmed by operator"


def test_cli_exposes_read_only_instant_buy_resolution_history() -> None:
    parser = cli_entry.build_parser()
    args = parser.parse_args([
        "instant-buy-resolutions",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
    ])
    assert args.command == "instant-buy-resolutions"
