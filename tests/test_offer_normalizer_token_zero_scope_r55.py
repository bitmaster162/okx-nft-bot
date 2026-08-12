from __future__ import annotations

import pytest

from okx_nft_bot.mass_offer.scanner import build_existing_offer_map
from okx_nft_bot.normalizers.offers import normalize_offer


COLLECTION = "0x0000000000000000000000000000000000000002"


def _okx_raw(token_id):
    return {
        "tokenId": token_id,
        "collectionAddress": COLLECTION,
        "orderId": "okx-order-r55",
        "price": "0.125",
        "amount": 1,
        "status": "active",
    }


@pytest.mark.parametrize("token_id", [0, "0"])
def test_r55_okx_zero_is_specific_token_offer(token_id):
    offer = normalize_offer(_okx_raw(token_id), market="okx", chain="bsc")

    assert offer.token_id == "0"
    assert offer.source_type == "token_offer"
    assert build_existing_offer_map([offer]) == {"0": 0.125}


@pytest.mark.parametrize("token_id", [None, ""])
def test_r55_okx_explicit_absence_stays_collection_offer(token_id):
    offer = normalize_offer(_okx_raw(token_id), market="okx", chain="bsc")

    assert offer.token_id is None
    assert offer.source_type == "collection_offer"


def _opensea_raw(*, item_type, identifier_or_criteria=None, identifier=None):
    item = {
        "itemType": item_type,
        "token": COLLECTION,
    }
    if identifier_or_criteria is not None:
        item["identifierOrCriteria"] = identifier_or_criteria
    if identifier is not None:
        item["identifier"] = identifier
    return {
        "order_hash": "0x" + "11" * 32,
        "current_price": "1000000000000000000",
        "maker": {"address": "0x0000000000000000000000000000000000000001"},
        "protocol_data": {
            "parameters": {
                "consideration": [item],
            }
        },
    }


@pytest.mark.parametrize("item_type", [2, "2", 3, "3"])
def test_r55_opensea_zero_identifier_is_specific_token_offer(item_type):
    offer = normalize_offer(
        _opensea_raw(item_type=item_type, identifier_or_criteria=0),
        market="opensea",
        chain="eth",
    )

    assert offer.token_id == "0"
    assert offer.source_type == "token_offer"
    assert offer.collection_slug_or_address == COLLECTION


def test_r55_opensea_zero_fallback_identifier_is_preserved():
    offer = normalize_offer(
        _opensea_raw(item_type=2, identifier=0),
        market="opensea",
        chain="eth",
    )

    assert offer.token_id == "0"
    assert offer.source_type == "token_offer"


@pytest.mark.parametrize("item_type", [4, "4", 5, "5"])
def test_r55_opensea_criteria_scope_stays_collection_offer(item_type):
    offer = normalize_offer(
        _opensea_raw(item_type=item_type, identifier_or_criteria=0),
        market="opensea",
        chain="eth",
    )

    assert offer.token_id is None
    assert offer.source_type == "collection_offer"


def test_r55_malformed_specific_item_does_not_widen_to_collection_scope():
    offer = normalize_offer(
        _opensea_raw(item_type=2),
        market="opensea",
        chain="eth",
    )

    assert offer.token_id is None
    assert offer.source_type == "token_offer"
