from __future__ import annotations

from copy import deepcopy

import pytest

from okx_nft_bot.sniper.sell_token_zero_safety import install_sell_token_zero_safety


class _Client:
    def __init__(self, *, wallet_response=None, listing_response=None) -> None:
        self.wallet_response = wallet_response
        self.listing_response = listing_response
        self.wallet_calls = 0
        self.listing_calls = 0
        self.effect_calls: list[tuple[str, tuple, dict]] = []

    def get_wallet_nfts(self, *args, **kwargs):
        self.wallet_calls += 1
        if isinstance(self.wallet_response, BaseException):
            raise self.wallet_response
        return self.wallet_response

    def get_listings(self, *args, **kwargs):
        self.listing_calls += 1
        if isinstance(self.listing_response, BaseException):
            raise self.listing_response
        return self.listing_response

    def create_listing(self, *args, **kwargs):
        self.effect_calls.append(("create_listing", args, kwargs))
        return {"status": "delegated"}

    def cancel_listing(self, *args, **kwargs):
        self.effect_calls.append(("cancel_listing", args, kwargs))
        return True


class _Bidder:
    def __init__(self, client: _Client) -> None:
        self.client = client

    def _get_okx_client(self):
        return self.client


def _guarded_bidder(client: _Client):
    class Bidder(_Bidder):
        pass

    install_sell_token_zero_safety(Bidder)
    return Bidder(client), Bidder


def _response(token_id):
    return {
        "code": "0",
        "data": {
            "data": [
                {"tokenId": token_id, "orderId": "order-1", "keep": "same"}
            ],
            "cursor": "next",
        },
        "outside": "same",
    }


def test_wallet_numeric_zero_becomes_truthy_string_for_legacy_sell_gate() -> None:
    source = _response(0)
    client = _Client(wallet_response=source)
    bidder, _ = _guarded_bidder(client)

    result = bidder._get_okx_client().get_wallet_nfts(chain="bsc")
    nft = result["data"]["data"][0]

    token_id = nft.get("tokenId") or nft.get("token_id", "")
    assert token_id == "0"
    assert bool(token_id) is True
    assert source["data"]["data"][0]["tokenId"] == 0


def test_listing_and_inventory_numeric_zero_share_same_item_key() -> None:
    wallet_source = _response(0)
    listing_source = _response(0)
    client = _Client(
        wallet_response=wallet_source,
        listing_response=listing_source,
    )
    bidder, _ = _guarded_bidder(client)
    proxied = bidder._get_okx_client()

    wallet_result = proxied.get_wallet_nfts(chain="bsc")
    listing_result = proxied.get_listings(chain="bsc", collection_address="0xabc")

    inventory_token = wallet_result["data"]["data"][0].get("tokenId") or ""
    listed_token = listing_result["data"]["data"][0].get("tokenId", "")
    our_listed_token_ids = {listed_token}

    assert inventory_token == "0"
    assert listed_token == "0"
    assert inventory_token in our_listed_token_ids
    assert wallet_source["data"]["data"][0]["tokenId"] == 0
    assert listing_source["data"]["data"][0]["tokenId"] == 0


def test_string_zero_stays_string_zero() -> None:
    client = _Client(wallet_response=_response("0"))
    bidder, _ = _guarded_bidder(client)

    result = bidder._get_okx_client().get_wallet_nfts()

    assert result["data"]["data"][0]["tokenId"] == "0"


@pytest.mark.parametrize("token_id", [None, "", False, 1, "7"])
def test_non_literal_zero_values_are_not_reclassified(token_id) -> None:
    source = _response(token_id)
    client = _Client(wallet_response=source)
    bidder, _ = _guarded_bidder(client)

    result = bidder._get_okx_client().get_wallet_nfts()

    assert result is source
    assert result["data"]["data"][0]["tokenId"] == token_id


def test_normalization_is_copy_on_write_and_preserves_unrelated_fields() -> None:
    source = _response(0)
    before = deepcopy(source)
    client = _Client(wallet_response=source)
    bidder, _ = _guarded_bidder(client)

    result = bidder._get_okx_client().get_wallet_nfts()

    assert result is not source
    assert result["data"] is not source["data"]
    assert result["data"]["data"] is not source["data"]["data"]
    assert result["outside"] == "same"
    assert result["data"]["cursor"] == "next"
    assert result["data"]["data"][0]["keep"] == "same"
    assert source == before


def test_effectful_methods_are_delegated_without_adaptation() -> None:
    client = _Client(wallet_response=_response(0), listing_response=_response(0))
    bidder, _ = _guarded_bidder(client)
    proxied = bidder._get_okx_client()

    create_result = proxied.create_listing("arg", token_id=0)
    cancel_result = proxied.cancel_listing("order-1")

    assert create_result == {"status": "delegated"}
    assert cancel_result is True
    assert client.effect_calls == [
        ("create_listing", ("arg",), {"token_id": 0}),
        ("cancel_listing", ("order-1",), {}),
    ]


def test_read_exceptions_propagate_unchanged() -> None:
    failure = RuntimeError("wallet read failed")
    client = _Client(wallet_response=failure)
    bidder, _ = _guarded_bidder(client)

    with pytest.raises(RuntimeError, match="wallet read failed"):
        bidder._get_okx_client().get_wallet_nfts()

    assert client.wallet_calls == 1


def test_malformed_read_shapes_are_returned_unchanged() -> None:
    for source in (None, [], {"data": []}, {"data": {"data": {}}}):
        client = _Client(wallet_response=source)
        bidder, _ = _guarded_bidder(client)
        assert bidder._get_okx_client().get_wallet_nfts() is source


def test_installer_is_idempotent() -> None:
    class Bidder(_Bidder):
        pass

    install_sell_token_zero_safety(Bidder)
    first = Bidder._get_okx_client
    install_sell_token_zero_safety(Bidder)

    assert Bidder._get_okx_client is first
    assert getattr(first, "_r58_sell_token_zero_scope", False) is True


def test_package_installation_preserves_r56_and_adds_r58() -> None:
    from okx_nft_bot.sniper import CounterBidder

    assert getattr(CounterBidder._raw_to_rival, "_r56_token_zero_scope", False) is True
    assert getattr(CounterBidder._get_okx_client, "_r58_sell_token_zero_scope", False) is True
