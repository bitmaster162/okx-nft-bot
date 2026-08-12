from __future__ import annotations

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient


OFFERER = "0x" + "1" * 40
COLLECTION = "0x" + "2" * 40
WETH = "0x" + "3" * 40


def _build(token_id):
    client = object.__new__(OpenSeaClient)
    return client._build_seaport_offer(
        offerer=OFFERER,
        collection_address=COLLECTION,
        token_id=token_id,
        price_wei=10**18,
        currency_address=WETH,
        counter=7,
        valid_time=2_000_000_000,
    )


@pytest.mark.parametrize("token_id", [0, "0"])
def test_r48_token_zero_remains_specific_erc721_item_offer(monkeypatch, token_id):
    monkeypatch.setenv("OPENSEA_FEE_BPS", "0")

    parameters = _build(token_id)
    nft = parameters["consideration"][0]

    assert nft["itemType"] == 2
    assert nft["identifierOrCriteria"] == 0
    assert nft["token"] == COLLECTION
    assert parameters["totalOriginalConsiderationItems"] == 1


@pytest.mark.parametrize("token_id", [None, ""])
def test_r48_explicit_absence_remains_collection_criteria_offer(monkeypatch, token_id):
    monkeypatch.setenv("OPENSEA_FEE_BPS", "0")

    parameters = _build(token_id)
    nft = parameters["consideration"][0]

    assert nft["itemType"] == 4
    assert nft["identifierOrCriteria"] == 0
    assert nft["token"] == COLLECTION
    assert parameters["totalOriginalConsiderationItems"] == 1


def test_r48_nonzero_item_offer_semantics_are_unchanged(monkeypatch):
    monkeypatch.setenv("OPENSEA_FEE_BPS", "0")

    parameters = _build("7")
    nft = parameters["consideration"][0]

    assert nft["itemType"] == 2
    assert nft["identifierOrCriteria"] == 7
    assert nft["token"] == COLLECTION
