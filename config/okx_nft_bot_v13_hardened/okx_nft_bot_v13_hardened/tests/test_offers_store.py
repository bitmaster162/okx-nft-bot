"""Unit tests for storage/offers_store.py"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore


def _make_offer(**overrides) -> NormalizedOffer:
    base = dict(
        market="okx",
        collection_slug_or_address="0xcollection",
        chain="bsc",
        token_id="1",
        offer_id="offer-001",
        maker="0xmaker",
        price=0.05,
        currency="WBNB",
        quantity=1,
        status="active",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        raw_payload_hash="abc123",
        observed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        source_type="token_offer",
        source_reliability="high",
    )
    base.update(overrides)
    return NormalizedOffer(**base)


@pytest.fixture()
def store(tmp_path: Path) -> OffersStore:
    return OffersStore(tmp_path / "offers.sqlite3")


def test_upsert_and_count(store: OffersStore):
    offers = [_make_offer(offer_id=f"o-{i}") for i in range(3)]
    inserted = store.upsert_offers(offers)
    assert inserted == 3
    assert store.count_offers() == 3


def test_upsert_idempotent(store: OffersStore):
    offer = _make_offer()
    store.upsert_offers([offer])
    store.upsert_offers([offer])  # second upsert should not increase count
    assert store.count_offers() == 1


def test_upsert_updates_price_on_conflict(store: OffersStore):
    store.upsert_offers([_make_offer(price=0.05)])
    store.upsert_offers([_make_offer(price=0.10)])
    results = store.query_offers(OfferFilters(limit=10))
    assert len(results) == 1
    assert results[0].price == pytest.approx(0.10)


def test_query_filter_by_market(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="okx-1", market="okx"),
        _make_offer(offer_id="os-1", market="opensea"),
    ])
    results = store.query_offers(OfferFilters(market="okx", limit=10))
    assert all(o.market == "okx" for o in results)
    assert len(results) == 1


def test_query_filter_by_chain(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="bsc-1", chain="bsc"),
        _make_offer(offer_id="eth-1", chain="eth"),
    ])
    results = store.query_offers(OfferFilters(chain="eth", limit=10))
    assert len(results) == 1
    assert results[0].chain == "eth"


def test_query_filter_by_maker(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="a", maker="0xalice"),
        _make_offer(offer_id="b", maker="0xbob"),
    ])
    results = store.query_offers(OfferFilters(maker="0xalice", limit=10))
    assert len(results) == 1
    assert results[0].maker == "0xalice"


def test_query_filter_min_max_price(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="cheap", price=0.01),
        _make_offer(offer_id="mid", price=0.05),
        _make_offer(offer_id="expensive", price=1.0),
    ])
    results = store.query_offers(OfferFilters(min_price=0.04, max_price=0.06, limit=10))
    assert len(results) == 1
    assert results[0].offer_id == "mid"


def test_query_active_only(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="active", status="active"),
        _make_offer(offer_id="cancelled", status="cancelled"),
    ])
    results = store.query_offers(OfferFilters(active_only=True, limit=10))
    assert len(results) == 1
    assert results[0].status == "active"


def test_query_collection_substring(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="pancake", collection_slug_or_address="0xdf7952b35f24acf7fc0487d01c8d5690a60dba07"),
        _make_offer(offer_id="other", collection_slug_or_address="0xother"),
    ])
    results = store.query_offers(OfferFilters(collection="0xdf7952", limit=10))
    assert len(results) == 1
    assert results[0].offer_id == "pancake"


def test_empty_upsert_returns_zero(store: OffersStore):
    assert store.upsert_offers([]) == 0


def test_count_by_market_and_status(store: OffersStore):
    store.upsert_offers([
        _make_offer(offer_id="a", market="okx", status="active"),
        _make_offer(offer_id="b", market="okx", status="cancelled"),
        _make_offer(offer_id="c", market="opensea", status="active"),
    ])
    assert store.count_offers(market="okx", status="active") == 1
    assert store.count_offers(market="okx") == 2
    assert store.count_offers(status="active") == 2
