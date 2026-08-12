from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.sniper.counter_bidder import CounterBidder


def _bare_bidder() -> CounterBidder:
    bidder = CounterBidder.__new__(CounterBidder)
    bidder._balance_cache = {}
    bidder._low_balance_alerted = set()
    return bidder


def test_get_balance_without_wallet_fails_closed(monkeypatch):
    bidder = _bare_bidder()

    monkeypatch.setattr(
        "okx_nft_bot.config.load_settings",
        lambda: SimpleNamespace(buyer_wallet_address=""),
    )

    with pytest.raises(RuntimeError, match="BUYER_WALLET_ADDRESS"):
        bidder._get_balance("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")


def test_preflight_balance_exception_blocks_offer():
    bidder = _bare_bidder()
    bidder._get_balance = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("rpc unavailable")
    )

    assert (
        bidder._check_balance_for_offer(
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "WETH",
            0.001,
        )
        is False
    )
