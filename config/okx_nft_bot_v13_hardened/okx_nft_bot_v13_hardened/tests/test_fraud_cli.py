from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from okx_nft_bot.cli import (
    cmd_analyze_asset,
    cmd_analyze_collection,
    cmd_analyze_wallet,
    cmd_sync_fraud_canon,
    cmd_watchlist_add,
    cmd_watchlist_show,
)
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.fraud_store import FraudStore
from okx_nft_bot.storage.sqlite import SQLiteStore


def _evt(
    *,
    event_id: str,
    event_type: str,
    at: datetime,
    price: float,
    maker: str | None = None,
    taker: str | None = None,
) -> NFTEvent:
    return NFTEvent(
        event_id=event_id,
        market="okx",
        event_type=event_type,  # type: ignore[arg-type]
        collection="Alpha",
        token_id="1",
        contract_address="0xabc",
        price=price,
        currency="ETH",
        quantity=1,
        maker=maker,
        taker=taker,
        tx_hash=f"tx-{event_id}",
        event_time=at,
        floor_price=price,
        raw_source="test",
    )


def test_sync_analyze_and_watchlist_cli_roundtrip(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "db.sqlite3"
    store = SQLiteStore(db_path)
    start = datetime(2026, 3, 23, 3, 0, tzinfo=timezone.utc)
    store.upsert_normalized_events(
        [
            _evt(event_id="listing-c1", event_type="listing", at=start, price=1.0, maker="0xseller"),
            _evt(event_id="listing-c2", event_type="listing", at=start + timedelta(minutes=20), price=0.97, maker="0xseller"),
            _evt(event_id="listing-c3", event_type="listing", at=start + timedelta(minutes=40), price=1.17, maker="0xseller"),
        ]
    )

    with patch("okx_nft_bot.cli.load_settings") as mock_settings:
        settings = MagicMock()
        settings.db_path = db_path
        mock_settings.return_value = settings

        assert cmd_sync_fraud_canon(market="okx", limit=None) == 0
        sync_payload = json.loads(capsys.readouterr().out)
        assert sync_payload["processed_events"] == 3

        assert cmd_analyze_collection(identifier="0xabc", sync_market="okx", sync_limit=None) == 0
        analysis_payload = json.loads(capsys.readouterr().out)
        assert analysis_payload["summary"]["object_type"] == "collection"
        assert "risk_assessment" in analysis_payload

        assert cmd_analyze_asset(
            asset_id=None,
            collection_identifier="0xabc",
            token_id="1",
            sync_market="okx",
            sync_limit=None,
        ) == 0
        asset_by_collection_payload = json.loads(capsys.readouterr().out)
        assert asset_by_collection_payload["summary"]["object_type"] == "asset"
        assert asset_by_collection_payload["summary"]["token_id"] == "1"

        asset_row = FraudStore(db_path).resolve_asset(collection_identifier="0xabc", token_id="1")
        assert asset_row is not None
        asset_id = asset_row["id"]
        assert cmd_analyze_asset(
            asset_id=asset_id,
            collection_identifier=None,
            token_id=None,
            sync_market="okx",
            sync_limit=None,
        ) == 0
        asset_by_id_payload = json.loads(capsys.readouterr().out)
        assert asset_by_id_payload["summary"]["id"] == asset_id

        assert cmd_analyze_wallet(identifier="0xseller", sync_market="okx", sync_limit=None) == 0
        wallet_payload = json.loads(capsys.readouterr().out)
        assert wallet_payload["summary"]["object_type"] == "wallet"
        assert wallet_payload["summary"]["wallet_address"] == "0xseller"

        assert cmd_watchlist_add(
            object_type="collection",
            identifier="0xabc",
            token_id=None,
            reason="monitor suspicious relist churn",
            priority="high",
            sync_market="okx",
            sync_limit=None,
        ) == 0
        watch_add_payload = json.loads(capsys.readouterr().out)
        assert watch_add_payload["object_type"] == "collection"
        assert watch_add_payload["priority"] == "high"

        assert cmd_watchlist_show(status="active") == 0
        watchlist_payload = json.loads(capsys.readouterr().out)
        assert len(watchlist_payload) == 1
        assert watchlist_payload[0]["object"]["contract_address"] == "0xabc"
        assert watchlist_payload[0]["risk_severity"] is not None
        assert watchlist_payload[0]["risk_summary"]["severity"] is not None
