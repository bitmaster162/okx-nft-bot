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


def test_release_for_retry_parser_requires_worker_stopped_attestation() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(_resolve_args("release-for-retry"))

    args = parser.parse_args(_resolve_args("release-for-retry") + ["--worker-stopped"])
    assert args.command == "resolve-instant-buy-claim"
    assert args.resolution == "release-for-retry"
    assert args.worker_stopped is True


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
