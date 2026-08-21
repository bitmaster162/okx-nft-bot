from __future__ import annotations

import pytest

from okx_nft_bot import cli_entry


WALLET = "0xabc0000000000000000000000000000000000090"
CHAIN = "bsc"
ORDER_ID = "order-r90"


def _resolve_args(resolution: str) -> list[str]:
    return [
        "resolve-instant-buy-claim",
        "--wallet",
        WALLET,
        "--chain",
        CHAIN,
        "--order-id",
        ORDER_ID,
        "--resolution",
        resolution,
        "--actor",
        "ops-r90",
        "--reason",
        "independent reconciliation of external outcome",
        "--yes",
    ]


def test_release_for_retry_parser_carries_worker_stopped_attestation() -> None:
    parser = cli_entry.build_parser()

    without_attestation = parser.parse_args(_resolve_args("release-for-retry"))
    assert without_attestation.command == "resolve-instant-buy-claim"
    assert without_attestation.resolution == "release-for-retry"
    assert without_attestation.worker_stopped is False

    with_attestation = parser.parse_args(
        _resolve_args("release-for-retry") + ["--worker-stopped"]
    )
    assert with_attestation.command == "resolve-instant-buy-claim"
    assert with_attestation.resolution == "release-for-retry"
    assert with_attestation.worker_stopped is True


def test_mark_completed_does_not_require_worker_stopped_attestation() -> None:
    args = cli_entry.build_parser().parse_args(_resolve_args("mark-completed"))
    assert args.command == "resolve-instant-buy-claim"
    assert args.resolution == "mark-completed"
    assert args.worker_stopped is False


def test_direct_retry_release_fails_closed_without_worker_stopped_attestation() -> None:
    with pytest.raises(SystemExit, match="worker-stopped"):
        cli_entry.cmd_resolve_instant_buy_claim(
            wallet=WALLET,
            chain=CHAIN,
            order_id=ORDER_ID,
            resolution="release-for-retry",
            worker_stopped=False,
            force=True,
            actor="ops-r90",
            reason="independent reconciliation found no completed effect",
        )
