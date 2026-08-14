from __future__ import annotations

from pathlib import Path

import pytest

from okx_nft_bot import cli_entry
from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0x" + "1" * 40
CHAIN = "eth"
ORDER_ID = "order-r76"


def _store(tmp_path: Path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "execution-r76.sqlite3")


def test_claims_are_enumerable_with_state_and_tx_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr76",
    )

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)

    assert len(rows) == 1
    assert rows[0]["wallet"] == WALLET.lower()
    assert rows[0]["chain"] == "eth"
    assert rows[0]["order_id"] == ORDER_ID
    assert rows[0]["state"] == "pending"
    assert rows[0]["tx_hash"] == "0xr76"


def test_mark_completed_keeps_terminal_identity_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, tx_hash="0xr76")

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="mark-completed",
    ) is True

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert rows[0]["tx_hash"] == "0xr76"
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is False


def test_release_for_retry_removes_claim_and_allows_new_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        tx_hash="0xr76-release",
    )

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
    ) is True
    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) == []
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True


def test_cli_exposes_read_and_explicit_resolution_commands() -> None:
    parser = cli_entry.build_parser()

    inspect_args = parser.parse_args([
        "instant-buy-claims",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
    ])
    assert inspect_args.command == "instant-buy-claims"

    resolve_args = parser.parse_args([
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
        "r76-test",
        "--reason",
        "r76 explicit reconciliation test",
        "--yes",
    ])
    assert resolve_args.command == "resolve-instant-buy-claim"
    assert resolve_args.yes is True


def test_mutating_cli_resolution_requires_yes() -> None:
    with pytest.raises(SystemExit, match="requires --yes"):
        cli_entry.cmd_resolve_instant_buy_claim(
            wallet=WALLET,
            chain=CHAIN,
            order_id=ORDER_ID,
            resolution="release-for-retry",
            force=False,
        )
