"""Unit tests for normalizers/offers.py"""
from __future__ import annotations

import json
import pytest

from okx_nft_bot.normalizers.offers import normalize_offer, _hash_payload


# ─── OKX ──────────────────────────────────────────────────────────────────────

def _okx_offer(**overrides):
    base = {
        "orderId": "order-123",
        "orderHash": "0xabc",
        "maker": "0xmaker",
        "collectionAddress": "0xcollection",
        "tokenId": "42",
        "price": "0.05",
        "currencyAddress": "0xWBNB",
        "status": "active",
        "amount": "1",
        "createTime": "1700000000000",
        "expirationTime": "1800000000000",
    }
    base.update(overrides)
    return base


def test_normalize_okx_basic():
    raw = _okx_offer()
    offer = normalize_offer(raw, market="okx", chain="bsc")
    assert offer.market == "okx"
    assert offer.chain == "bsc"
    assert offer.offer_id == "order-123"
    assert offer.maker == "0xmaker"
    assert offer.collection_slug_or_address == "0xcollection"
    assert offer.token_id == "42"
    assert offer.price == pytest.approx(0.05)
    assert offer.currency == "0xWBNB"
    assert offer.status == "active"
    assert offer.source_type == "token_offer"
    assert offer.source_reliability == "high"


def test_normalize_okx_collection_offer():
    raw = _okx_offer(tokenId=None)
    del raw["tokenId"]
    offer = normalize_offer(raw, market="okx", chain="bsc")
    assert offer.token_id is None
    assert offer.source_type == "collection_offer"


def test_normalize_okx_timestamps():
    raw = _okx_offer(createTime="1700000000000", expirationTime="1800000000000")
    offer = normalize_offer(raw, market="okx", chain="bsc")
    assert offer.created_at is not None
    assert offer.expires_at is not None
    assert offer.created_at.year >= 2023


def test_normalize_okx_missing_price():
    raw = _okx_offer(price=None)
    raw["price"] = None
    offer = normalize_offer(raw, market="okx", chain="bsc")
    assert offer.price is None


def test_normalize_okx_uses_orderId_fallback():
    raw = _okx_offer()
    del raw["orderId"]
    raw["orderHash"] = "0xhash"
    offer = normalize_offer(raw, market="okx", chain="bsc")
    assert offer.offer_id == "0xhash"


def test_normalize_okx_payload_hash_is_deterministic():
    raw = _okx_offer()
    h1 = _hash_payload(raw)
    h2 = _hash_payload(raw)
    assert h1 == h2
    assert len(h1) == 16


def test_normalize_okx_different_payloads_have_different_hashes():
    h1 = _hash_payload(_okx_offer(price="0.05"))
    h2 = _hash_payload(_okx_offer(price="0.06"))
    assert h1 != h2


# ─── OpenSea ──────────────────────────────────────────────────────────────────

def _opensea_offer(**overrides):
    base = {
        "order_hash": "os-order-456",
        "maker": {"address": "0xos_maker"},
        "current_price": "50000000000000000",  # 0.05 ETH in wei
        "expiration_time": 1800000000,
        "listing_time": 1700000000,
        "protocol_data": {
            "parameters": {
                "consideration": [
                    {
                        "itemType": 2,
                        "token": "0xos_collection",
                        "identifierOrCriteria": "99",
                    }
                ]
            }
        },
    }
    base.update(overrides)
    return base


def test_normalize_opensea_basic():
    raw = _opensea_offer()
    offer = normalize_offer(raw, market="opensea", chain="ethereum")
    assert offer.market == "opensea"
    assert offer.chain == "ethereum"
    assert offer.offer_id == "os-order-456"
    assert offer.maker == "0xos_maker"
    assert offer.collection_slug_or_address == "0xos_collection"
    assert offer.token_id == "99"
    assert offer.source_type == "token_offer"
    assert offer.source_reliability == "high"
    # price should be converted from wei
    assert offer.price is not None
    assert offer.price == pytest.approx(0.05, abs=1e-9)


def test_normalize_opensea_collection_offer():
    raw = _opensea_offer()
    raw["protocol_data"]["parameters"]["consideration"] = [
        {"itemType": 4, "token": "0xos_collection", "identifierOrCriteria": "0"}
    ]
    offer = normalize_offer(raw, market="opensea", chain="ethereum")
    assert offer.token_id is None
    assert offer.source_type == "collection_offer"


def test_normalize_opensea_maker_string():
    raw = _opensea_offer(maker="0xraw_maker_str")
    offer = normalize_offer(raw, market="opensea", chain="ethereum")
    assert offer.maker == "0xraw_maker_str"


def test_normalize_unknown_market_raises():
    with pytest.raises(ValueError, match="Unsupported market"):
        normalize_offer({}, market="magiceden_xyz", chain="eth")
