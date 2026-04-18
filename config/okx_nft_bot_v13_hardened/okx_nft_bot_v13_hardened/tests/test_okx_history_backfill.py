from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from okx_nft_bot.cli import (
    cmd_fetch_magiceden_actions_history,
    cmd_fetch_okx_actions_history,
    cmd_fetch_okx_sales_history,
    cmd_fetch_opensea_actions_history,
)
from okx_nft_bot.history_backfill import (
    backfill_magiceden_actions_history,
    backfill_okx_actions_history,
    backfill_okx_sales_history,
    backfill_opensea_actions_history,
)
from okx_nft_bot.storage.sqlite import SQLiteStore


class FakeOKXHistoryClient:
    def get_collection_list(self, *, chain: str | None = None, limit: int | None = None, cursor: str | None = None) -> dict:
        assert chain == "eth"
        if cursor is None:
            return {
                "code": 0,
                "data": {
                    "cursor": "page-2",
                    "next": True,
                    "data": [
                        {
                            "name": "Alpha",
                            "collectionAddress": "0xabc",
                            "floorPrice": 1.2,
                            "volume24h": 25.0,
                        },
                        {
                            "name": "SkipMe",
                        },
                    ],
                },
            }
        return {
            "code": 0,
            "data": {
                "cursor": None,
                "next": False,
                "data": [
                    {
                        "collection": {
                            "name": "Beta",
                            "assetContracts": [{"contractAddress": "0xdef"}],
                        }
                    }
                ],
            },
        }

    def get_collection_trades(
        self,
        *,
        chain: str,
        collection_address: str,
        platform: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict:
        assert chain == "eth"
        assert platform == "OKX"
        assert start_time == "1700000000"
        assert end_time == "1700003600"
        if collection_address == "0xabc":
            if cursor is None:
                return {
                    "code": 0,
                    "data": {
                        "cursor": "trades-2",
                        "next": True,
                        "data": [
                            {
                                "collectionAddress": "0xabc",
                                "currencyAddress": "ETH",
                                "from": "0xseller",
                                "to": "0xbuyer",
                                "price": 1.5,
                                "amount": 1,
                                "tokenId": "1",
                                "timestamp": 1700000100,
                                "txHash": "0xtrade1",
                            }
                        ],
                    },
                }
            return {
                "code": 0,
                "data": {
                    "cursor": None,
                    "next": False,
                    "data": [
                        {
                            "collectionAddress": "0xabc",
                            "currencyAddress": "ETH",
                            "from": "0xseller2",
                            "to": "0xbuyer2",
                            "price": 1.7,
                            "amount": 1,
                            "tokenId": "2",
                            "timestamp": 1700000200,
                            "txHash": "0xtrade2",
                        }
                    ],
                },
            }
        return {
            "code": 0,
            "data": {
                "cursor": None,
                "next": False,
                "data": [],
            },
        }


class FakeOpenSeaHistoryClient:
    def get_collection(self, *, slug: str) -> dict:
        assert slug == "pudgy-penguins"
        return {"name": "Pudgy Penguins"}

    def get_collection_stats(self, *, slug: str) -> dict:
        assert slug == "pudgy-penguins"
        return {"floor_price": 10.5, "total": {"one_day": 120.0}}

    def get_collection_events(
        self,
        *,
        slug: str,
        event_type: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict:
        assert slug == "pudgy-penguins"
        assert limit == 50
        if event_type == "sale":
            return {
                "asset_events": [
                    {
                        "event_id": "sale-1",
                        "event_timestamp": "2026-03-23T00:00:00Z",
                        "sale_price": "1.5",
                        "payment": {"symbol": "ETH"},
                        "seller": {"address": "0xseller"},
                        "buyer": {"address": "0xbuyer"},
                        "transaction": "0xtx1",
                        "nft": {"identifier": "12", "contract": "0xabc"},
                    }
                ],
                "next": None,
            }
        if event_type == "listing":
            return {
                "asset_events": [
                    {
                        "event_id": "listing-1",
                        "created_date": "2026-03-23T00:05:00Z",
                        "price": {"current": "1.7", "currency": "ETH"},
                        "seller": {"address": "0xseller"},
                        "nft": {"identifier": "13", "contract": "0xabc"},
                    }
                ],
                "next": None,
            }
        raise AssertionError(event_type)


class FakeMagicEdenHistoryClient:
    def get_collection_activity(
        self,
        *,
        chain: str,
        collection: str,
        types: list[str] | None = None,
        continuation: str | None = None,
        limit: int = 20,
    ) -> dict:
        assert chain == "ethereum"
        assert collection == "0xme"
        assert types == ["sale", "ask"]
        assert limit == 50
        if continuation is None:
            return {
                "activities": [
                    {
                        "id": "act-sale-1",
                        "type": "sale",
                        "contract": "0xme",
                        "fromAddress": "0xseller",
                        "toAddress": "0xbuyer",
                        "txHash": "0xmetx1",
                        "timestamp": 1700000100,
                        "token": {
                            "tokenId": "21",
                            "contract": "0xme",
                            "collection": {"name": "ME Alpha"},
                        },
                        "price": {
                            "amount": {"native": 2.5},
                            "currency": {"symbol": "ETH"},
                        },
                    }
                ],
                "continuation": "page-2",
            }
        return {
            "activities": [
                {
                    "id": "act-ask-1",
                    "type": "ask",
                    "contract": "0xme",
                    "maker": "0xlister",
                    "tokenSetId": "token:0xme:22",
                    "createdAt": "2026-03-23T00:10:00Z",
                    "price": {
                        "amount": {"native": 2.8},
                        "currency": {"symbol": "ETH"},
                    },
                }
            ],
            "continuation": None,
        }


def test_backfill_okx_sales_history_writes_events(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    store = SQLiteStore(db_path)
    result = backfill_okx_sales_history(
        client=FakeOKXHistoryClient(),
        store=store,
        chain="eth",
        platform="OKX",
        start_time="1700000000",
        end_time="1700003600",
        collection_page_limit=300,
        trade_page_limit=50,
    )

    assert result["processed_collections"] == 2
    assert result["skipped_collections"] == 1
    assert result["collections_with_sales"] == 1
    assert result["raw_events_written"] == 2
    assert result["normalized_events_written"] == 2
    assert result["new_normalized_events"] == 2
    assert result["errors"] == []
    assert store.count_events() == 2

    with sqlite3.connect(db_path) as conn:
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        rows = conn.execute(
            """
            SELECT collection_name, token_id, contract_address, price, maker, taker, raw_source
            FROM normalized_events
            ORDER BY event_time
            """
        ).fetchall()

    assert raw_count == 2
    assert rows[0] == ("Alpha", "1", "0xabc", 1.5, "0xseller", "0xbuyer", "okx_marketplace_history")
    assert rows[1] == ("Alpha", "2", "0xabc", 1.7, "0xseller2", "0xbuyer2", "okx_marketplace_history")


def test_backfill_okx_actions_history_alias(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    store = SQLiteStore(db_path)

    result = backfill_okx_actions_history(
        client=FakeOKXHistoryClient(),
        store=store,
        chain="eth",
        platform="OKX",
        start_time="1700000000",
        end_time="1700003600",
    )

    assert result["event_types"] == ["sale"]
    assert result["raw_events_written"] == 2


def test_backfill_opensea_actions_history_writes_events(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    store = SQLiteStore(db_path)

    result = backfill_opensea_actions_history(
        client=FakeOpenSeaHistoryClient(),
        store=store,
        slug="pudgy-penguins",
        event_types=["sale", "listing"],
        limit=50,
    )

    assert result["processed_pages"] == 2
    assert result["raw_events_written"] == 2
    assert result["normalized_events_written"] == 2
    assert result["new_normalized_events"] == 2
    assert result["errors"] == []

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT market, event_type, collection_name, token_id, contract_address, raw_source
            FROM normalized_events
            ORDER BY event_time
            """
        ).fetchall()

    assert rows == [
        ("opensea", "sale", "Pudgy Penguins", "12", "0xabc", "opensea_collection_history"),
        ("opensea", "listing", "Pudgy Penguins", "13", "0xabc", "opensea_collection_history"),
    ]


def test_backfill_magiceden_actions_history_writes_events(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    store = SQLiteStore(db_path)

    result = backfill_magiceden_actions_history(
        client=FakeMagicEdenHistoryClient(),
        store=store,
        chain="ethereum",
        collection="0xme",
        types=["sale", "ask"],
        limit=50,
    )

    assert result["processed_pages"] == 2
    assert result["raw_events_written"] == 2
    assert result["normalized_events_written"] == 2
    assert result["new_normalized_events"] == 2

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT market, event_type, collection_name, token_id, contract_address, raw_source
            FROM normalized_events
            ORDER BY event_type, token_id
            """
        ).fetchall()

    assert rows == [
        ("magiceden", "listing", "0xme", "22", "0xme", "magiceden_activity_history"),
        ("magiceden", "sale", "ME Alpha", "21", "0xme", "magiceden_activity_history"),
    ]


def test_cli_fetch_okx_sales_history_smoke(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "db.sqlite3"
    SQLiteStore(db_path)

    with patch("okx_nft_bot.cli.load_settings") as mock_settings, patch("okx_nft_bot.cli.OKXMarketplaceClient", return_value=FakeOKXHistoryClient()):
        settings = MagicMock()
        settings.db_path = db_path
        mock_settings.return_value = settings

        assert cmd_fetch_okx_sales_history(
            chain="eth",
            start_time="1700000000",
            end_time="1700003600",
            platform="OKX",
            collection_page_limit=300,
            trade_page_limit=50,
            max_collections=None,
            max_trade_pages_per_collection=None,
        ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["processed_collections"] == 2
    assert payload["collections_with_sales"] == 1
    assert payload["new_normalized_events"] == 2


def test_cli_fetch_okx_actions_history_smoke(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "db.sqlite3"
    SQLiteStore(db_path)

    with patch("okx_nft_bot.cli.load_settings") as mock_settings, patch("okx_nft_bot.cli.OKXMarketplaceClient", return_value=FakeOKXHistoryClient()):
        settings = MagicMock()
        settings.db_path = db_path
        mock_settings.return_value = settings

        assert cmd_fetch_okx_actions_history(
            chain="eth",
            start_time="1700000000",
            end_time="1700003600",
            platform="OKX",
            collection_page_limit=300,
            trade_page_limit=50,
            max_collections=None,
            max_trade_pages_per_collection=None,
        ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["event_types"] == ["sale"]
    assert payload["new_normalized_events"] == 2


def test_cli_fetch_opensea_actions_history_smoke(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "db.sqlite3"
    SQLiteStore(db_path)

    with patch("okx_nft_bot.cli.load_settings") as mock_settings, patch("okx_nft_bot.cli.OpenSeaClient", return_value=FakeOpenSeaHistoryClient()):
        settings = MagicMock()
        settings.db_path = db_path
        mock_settings.return_value = settings

        assert cmd_fetch_opensea_actions_history(
            slug="pudgy-penguins",
            event_types=["sale", "listing"],
            limit=50,
            max_pages_per_type=None,
        ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["market"] == "opensea"
    assert payload["new_normalized_events"] == 2


def test_cli_fetch_magiceden_actions_history_smoke(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "db.sqlite3"
    SQLiteStore(db_path)

    with patch("okx_nft_bot.cli.load_settings") as mock_settings, patch("okx_nft_bot.cli.MagicEdenClient", return_value=FakeMagicEdenHistoryClient()):
        settings = MagicMock()
        settings.db_path = db_path
        mock_settings.return_value = settings

        assert cmd_fetch_magiceden_actions_history(
            chain="ethereum",
            collection="0xme",
            types=["sale", "ask"],
            limit=50,
            max_pages=None,
        ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["market"] == "magiceden"
    assert payload["new_normalized_events"] == 2
