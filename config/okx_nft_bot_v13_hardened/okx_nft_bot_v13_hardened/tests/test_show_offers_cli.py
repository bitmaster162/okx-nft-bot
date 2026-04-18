"""CLI smoke tests for offers commands and regression tests for analytics commands."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from okx_nft_bot.cli import cmd_detect_spreads, cmd_fetch_offers, cmd_rank_collections, cmd_show_offers
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore


def _make_offer(offer_id: str = "o1", market: str = "okx", status: str = "active") -> NormalizedOffer:
    return NormalizedOffer(
        market=market,
        collection_slug_or_address="0xcollection",
        chain="bsc",
        token_id="1",
        offer_id=offer_id,
        maker="0xmaker",
        price=0.05,
        currency="WBNB",
        quantity=1,
        status=status,
        raw_payload_hash="abc",
        observed_at=datetime.now(timezone.utc),
        source_type="token_offer",
        source_reliability="high",
    )


@pytest.fixture()
def offers_db(tmp_path: Path) -> Path:
    db = tmp_path / "offers.sqlite3"
    store = OffersStore(db)
    store.upsert_offers([
        _make_offer("offer-okx-1", market="okx", status="active"),
        _make_offer("offer-okx-2", market="okx", status="cancelled"),
        _make_offer("offer-os-1", market="opensea", status="active"),
    ])
    return db


def test_show_offers_returns_all(offers_db: Path, capsys, tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store

            rc = cmd_show_offers(
                market=None,
                collection=None,
                chain=None,
                maker=None,
                status=None,
                min_price=None,
                max_price=None,
                active_only=False,
                limit=50,
            )

    assert rc == 0
    MockStore.assert_called_once_with(offers_db)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 3


def test_show_offers_filters_by_market(offers_db: Path, capsys, tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store

            rc = cmd_show_offers(
                market="opensea",
                collection=None,
                chain=None,
                maker=None,
                status=None,
                min_price=None,
                max_price=None,
                active_only=False,
                limit=50,
            )

    assert rc == 0
    MockStore.assert_called_once_with(offers_db)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["market"] == "opensea"


def test_show_offers_active_only(offers_db: Path, capsys, tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store

            rc = cmd_show_offers(
                market=None,
                collection=None,
                chain=None,
                maker=None,
                status=None,
                min_price=None,
                max_price=None,
                active_only=True,
                limit=50,
            )

    assert rc == 0
    MockStore.assert_called_once_with(offers_db)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert all(o["status"] == "active" for o in data)
    assert len(data) == 2


def test_fetch_offers_uses_configured_offers_db_path(capsys, tmp_path):
    offers_db = tmp_path / "custom-offers.sqlite3"
    offer = _make_offer("offer-okx-fetch")

    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OKXMarketplaceClient") as MockClient, \
             patch("okx_nft_bot.cli.OKXOffersProvider") as MockProvider, \
             patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store
            provider = MockProvider.return_value
            provider.fetch_all_pages.return_value = [offer]

            rc = cmd_fetch_offers(
                market="okx",
                chain="eth",
                maker="0xmaker",
                collection_address="0xcollection",
                slug=None,
                collection=None,
                max_pages=3,
            )

    assert rc == 0
    MockClient.assert_called_once_with(settings)
    MockProvider.assert_called_once_with(client=MockClient.return_value, settings=settings)
    provider.fetch_all_pages.assert_called_once_with(
        chain="eth",
        collection_address="0xcollection",
        maker="0xmaker",
        max_pages=3,
    )
    MockStore.assert_called_once_with(offers_db)
    stored = OffersStore(offers_db).query_offers(OfferFilters(limit=10))
    assert len(stored) == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["market"] == "okx"
    assert data["fetched"] == 1
    assert data["stored"] == 1


def test_fetch_opensea_offers_uses_opensea_provider_and_store(capsys, tmp_path):
    offers_db = tmp_path / "custom-offers.sqlite3"
    offer = _make_offer("offer-os-fetch", market="opensea")

    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OpenSeaClient") as MockClient, \
             patch("okx_nft_bot.cli.OpenSeaOffersProvider") as MockProvider, \
             patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store
            provider = MockProvider.return_value
            provider.fetch_all_pages.return_value = [offer]

            rc = cmd_fetch_offers(
                market="opensea",
                chain="ethereum",
                maker=None,
                collection_address=None,
                slug="pudgy-penguins",
                collection=None,
                max_pages=2,
            )

    assert rc == 0
    MockClient.assert_called_once_with(settings)
    MockProvider.assert_called_once_with(client=MockClient.return_value, settings=settings)
    provider.fetch_all_pages.assert_called_once_with(
        slug="pudgy-penguins",
        chain="ethereum",
        max_pages=2,
    )
    MockStore.assert_called_once_with(offers_db)
    stored = OffersStore(offers_db).query_offers(OfferFilters(limit=10))
    assert len(stored) == 1
    assert stored[0].market == "opensea"
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["market"] == "opensea"
    assert data["fetched"] == 1
    assert data["stored"] == 1
    assert data["collection"] == "pudgy-penguins"


def test_fetch_okx_offers_requires_collection_address(tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = tmp_path / "offers.sqlite3"
        mock_settings.return_value = settings

        with pytest.raises(SystemExit, match="--collection-address is required for market okx"):
            cmd_fetch_offers(
                market="okx",
                chain="eth",
                maker=None,
                collection_address=None,
                slug=None,
                collection=None,
                max_pages=1,
            )


def test_fetch_opensea_offers_requires_slug(tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = tmp_path / "offers.sqlite3"
        mock_settings.return_value = settings

        with pytest.raises(SystemExit, match="--slug is required for market opensea"):
            cmd_fetch_offers(
                market="opensea",
                chain=None,
                maker=None,
                collection_address=None,
                slug=None,
                collection=None,
                max_pages=1,
            )


def test_fetch_opensea_offers_rejects_okx_only_args(tmp_path):
    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = tmp_path / "offers.sqlite3"
        mock_settings.return_value = settings

        with pytest.raises(SystemExit, match="--maker is only valid for market okx"):
            cmd_fetch_offers(
                market="opensea",
                chain="ethereum",
                maker="0xmaker",
                collection_address=None,
                slug="pudgy-penguins",
                collection=None,
                max_pages=1,
            )


def test_fetch_okx_offers_accepts_legacy_collection_alias(capsys, tmp_path):
    offers_db = tmp_path / "custom-offers.sqlite3"
    offer = _make_offer("offer-okx-legacy")

    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        settings.offers_db_path = offers_db
        mock_settings.return_value = settings

        with patch("okx_nft_bot.cli.OKXMarketplaceClient") as MockClient, \
             patch("okx_nft_bot.cli.OKXOffersProvider") as MockProvider, \
             patch("okx_nft_bot.cli.OffersStore") as MockStore:
            real_store = OffersStore(offers_db)
            MockStore.return_value = real_store
            provider = MockProvider.return_value
            provider.fetch_all_pages.return_value = [offer]

            rc = cmd_fetch_offers(
                market="okx",
                chain="eth",
                maker=None,
                collection_address=None,
                slug=None,
                collection="0xlegacy",
                max_pages=1,
            )

    assert rc == 0
    provider.fetch_all_pages.assert_called_once_with(
        chain="eth",
        collection_address="0xlegacy",
        maker=None,
        max_pages=1,
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["collection"] == "0xlegacy"


def test_detect_spreads_with_empty_db(capsys, tmp_path):
    """detect-spreads should not crash on an empty database."""
    from okx_nft_bot.storage.sqlite import SQLiteStore

    db = SQLiteStore(tmp_path / "events.sqlite3")

    with patch("okx_nft_bot.cli.load_settings") as ms, \
         patch("okx_nft_bot.cli.SQLiteStore") as mstore:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        ms.return_value = settings
        mstore.return_value = db

        rc = cmd_detect_spreads(min_pct=3.0, limit=10, sample_limit=5000)

    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)


def test_rank_collections_includes_binance_fields(capsys, tmp_path):
    """rank-collections output should include Binance support fields."""
    from okx_nft_bot.models import NFTEvent
    from okx_nft_bot.storage.sqlite import SQLiteStore

    db = SQLiteStore(tmp_path / "events.sqlite3")
    event = NFTEvent(
        event_id="e1",
        market="okx",
        event_type="sale",
        collection="Pancake Bunnies",
        token_id="1",
        contract_address="0xdf7952b35f24acf7fc0487d01c8d5690a60dba07",
        price=1.0,
        currency="BNB",
        quantity=1,
        event_time=datetime.now(timezone.utc),
        raw_source="test",
    )
    db.upsert_normalized_events([event])

    with patch("okx_nft_bot.cli.load_settings") as ms, \
         patch("okx_nft_bot.cli.SQLiteStore") as mstore:
        settings = MagicMock()
        settings.db_path = tmp_path / "events.sqlite3"
        ms.return_value = settings
        mstore.return_value = db

        rc = cmd_rank_collections(min_pct=0.0, limit=10, sample_limit=5000)

    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    row = data[0]
    assert "binance_supported" in row
    assert "binance_list_type" in row
    assert "support_confidence" in row
    assert "freshness" in row
    assert "confidence" in row
