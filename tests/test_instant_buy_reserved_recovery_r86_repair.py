from __future__ import annotations

import pytest

from okx_nft_bot import cli_entry
from okx_nft_bot.sniper.durable_pending_effect import DurablePendingEffectStore


WALLET = "0xabc0000000000000000000000000000000000086"
CHAIN = "bsc"
ORDER_ID = "order-r86"


def _store(tmp_path) -> DurablePendingEffectStore:
    return DurablePendingEffectStore(tmp_path / "r86.sqlite3")


def test_reserved_claim_can_move_to_pending_only_as_an_audited_transition(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    assert store.mark_reserved_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        actor="ops-r86",
        reason="worker process confirmed stopped; effect outcome remains unknown",
    ) is True

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"

    history = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10)
    assert len(history) == 1
    assert history[0]["prior_state"] == "reserved"
    assert history[0]["resolution"] == "mark-pending"
    assert history[0]["actor"] == "ops-r86"
    assert history[0]["reason"] == "worker process confirmed stopped; effect outcome remains unknown"


def test_reserved_claim_still_cannot_be_released_directly_and_retry_release_is_separate(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor="ops-r86",
        reason="must remain blocked while worker may still be live",
    ) is False

    assert store.mark_reserved_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        actor="ops-r86",
        reason="worker process confirmed stopped; reconcile outcome separately",
    ) is True

    rows = store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"

    assert store.resolve_claim(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        resolution="release-for-retry",
        actor="ops-r86-reviewer",
        reason="independent reconciliation found no completed effect",
    ) is True
    assert store.fetch_claims(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10) == []

    history = store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10)
    assert [row["resolution"] for row in history] == ["release-for-retry", "mark-pending"]


def test_mark_reserved_pending_is_state_guarded_and_does_not_invent_history(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True
    store.mark_pending(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID)

    assert store.mark_reserved_pending(
        wallet=WALLET,
        chain=CHAIN,
        order_id=ORDER_ID,
        actor="ops-r86",
        reason="should not overwrite an existing pending claim",
    ) is False
    assert store.fetch_resolutions(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID, limit=10) == []


def test_mark_reserved_pending_requires_explicit_provenance(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.reserve(wallet=WALLET, chain=CHAIN, order_id=ORDER_ID) is True

    with pytest.raises(ValueError, match="actor and reason"):
        store.mark_reserved_pending(
            wallet=WALLET,
            chain=CHAIN,
            order_id=ORDER_ID,
            actor="",
            reason="",
        )


def test_cli_requires_worker_stopped_attestation_for_reserved_to_pending_transition() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([
            "mark-instant-buy-claim-pending",
            "--wallet",
            WALLET,
            "--chain",
            CHAIN,
            "--order-id",
            ORDER_ID,
            "--actor",
            "ops-r86",
            "--reason",
            "worker process confirmed stopped",
            "--yes",
        ])

    args = parser.parse_args([
        "mark-instant-buy-claim-pending",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
        "--worker-stopped",
        "--actor",
        "ops-r86",
        "--reason",
        "worker process confirmed stopped",
        "--yes",
    ])
    assert args.command == "mark-instant-buy-claim-pending"
    assert args.worker_stopped is True
    assert args.actor == "ops-r86"
    assert args.reason == "worker process confirmed stopped"
    assert args.yes is True


def test_direct_cli_transition_fails_closed_without_worker_stopped_attestation() -> None:
    with pytest.raises(SystemExit, match="worker-stopped"):
        cli_entry.cmd_mark_instant_buy_claim_pending(
            wallet=WALLET,
            chain=CHAIN,
            order_id=ORDER_ID,
            worker_stopped=False,
            force=True,
            actor="ops-r86",
            reason="worker process confirmed stopped",
        )
