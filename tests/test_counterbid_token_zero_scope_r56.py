from __future__ import annotations

import pytest

from okx_nft_bot.sniper import CounterBidder
from okx_nft_bot.sniper.token_zero_scope_safety import install_token_zero_scope_safety


_COLLECTION = "0x1111111111111111111111111111111111111111"
_MAKER = "0x2222222222222222222222222222222222222222"


def _bidder() -> CounterBidder:
    bidder = CounterBidder.__new__(CounterBidder)
    bidder._wl_index = {}
    return bidder


def _raw(token_id):
    return {
        "collectionAddress": _COLLECTION,
        "collectionName": "Token Zero",
        "tokenId": token_id,
        "price": "1",
        "currency": "ETH",
        "maker": _MAKER,
        "orderId": "order-r56",
    }


@pytest.mark.parametrize("token_id", [0, "0"])
def test_raw_token_zero_is_preserved_as_item_scope(token_id) -> None:
    offer = _bidder()._raw_to_rival(_raw(token_id), "eth")

    assert offer.token_id == "0"
    assert offer.source_type == "token_offer"
    assert offer.collection_address == _COLLECTION
    assert offer.offer_id == "order-r56"


@pytest.mark.parametrize("token_id", [None, "", False])
def test_explicit_absence_and_false_are_not_promoted_to_token_zero(token_id) -> None:
    offer = _bidder()._raw_to_rival(_raw(token_id), "eth")

    assert offer.token_id == ""
    assert offer.source_type == "collection_offer"


@pytest.mark.parametrize("token_id", [7, "7"])
def test_nonzero_item_scope_is_unchanged(token_id) -> None:
    offer = _bidder()._raw_to_rival(_raw(token_id), "eth")

    assert offer.token_id == "7"
    assert offer.source_type == "token_offer"


def test_token_zero_survives_into_submit_boundary() -> None:
    bidder = _bidder()
    offer = bidder._raw_to_rival(_raw(0), "eth")
    captured: dict[str, object] = {}

    def fake_submit_eth(
        collection_address,
        token_id,
        price,
        currency,
        *,
        quantity=1,
        duration_hours=720,
    ):
        captured.update(
            collection_address=collection_address,
            token_id=token_id,
            price=price,
            currency=currency,
            quantity=quantity,
            duration_hours=duration_hours,
        )
        return False

    bidder._submit_eth = fake_submit_eth

    ok = bidder._submit_undercut(
        _COLLECTION,
        offer.token_id or "",
        1.0,
        "WETH",
        "eth",
        quantity=1,
        duration_hours=24,
    )

    assert ok is False
    assert captured["token_id"] == "0"
    assert captured["collection_address"] == _COLLECTION


def test_r56_installer_is_active_and_idempotent() -> None:
    current = CounterBidder._raw_to_rival
    assert getattr(current, "_r56_token_zero_scope", False) is True

    install_token_zero_scope_safety(CounterBidder)

    assert CounterBidder._raw_to_rival is current
