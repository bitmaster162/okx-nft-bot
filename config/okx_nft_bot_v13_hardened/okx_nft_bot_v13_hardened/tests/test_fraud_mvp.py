from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from okx_nft_bot.fraud.materialize import materialize_from_normalized_events
from okx_nft_bot.fraud.reporting import build_asset_report, build_collection_report
from okx_nft_bot.fraud.scoring import compute_risk_score
from okx_nft_bot.models import NFTEvent
from okx_nft_bot.storage.fraud_store import FraudStore
from okx_nft_bot.storage.sqlite import SQLiteStore


def _evt(
    *,
    event_id: str,
    event_type: str,
    at: datetime,
    collection: str = "Alpha",
    token_id: str = "1",
    contract_address: str = "0xabc",
    price: float | None = None,
    maker: str | None = None,
    taker: str | None = None,
    floor_price: float | None = None,
) -> NFTEvent:
    return NFTEvent(
        event_id=event_id,
        market="okx",
        event_type=event_type,  # type: ignore[arg-type]
        collection=collection,
        token_id=token_id,
        contract_address=contract_address,
        price=price,
        currency="ETH",
        quantity=1,
        maker=maker,
        taker=taker,
        tx_hash=f"tx-{event_id}",
        event_time=at,
        floor_price=floor_price,
        raw_source="test",
    )


def test_sync_materializes_canonical_tables_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    event_store = SQLiteStore(db_path)
    fraud_store = FraudStore(db_path)
    start = datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(event_id="listing-1", event_type="listing", at=start, price=1.0, maker="0xseller", floor_price=1.0),
        _evt(event_id="sale-1", event_type="sale", at=start + timedelta(minutes=30), price=1.1, maker="0xseller", taker="0xbuyer"),
    ]
    event_store.upsert_normalized_events(events)

    first = materialize_from_normalized_events(event_store=event_store, fraud_store=fraud_store, market="okx")
    second = materialize_from_normalized_events(event_store=event_store, fraud_store=fraud_store, market="okx")

    assert first["processed_events"] == 2
    assert second["processed_events"] == 2
    counts = fraud_store.table_counts()
    assert counts["collections"] == 1
    assert counts["assets"] == 1
    assert counts["listings"] == 1
    assert counts["sales"] == 1
    assert counts["entities"] == 2
    assert counts["floor_snapshots"] == 1

    asset = fraud_store.resolve_asset(collection_identifier="0xabc", token_id="1")
    assert asset is not None
    listings = fraud_store.fetch_asset_listings(asset["id"])
    assert len(listings) == 1
    assert listings[0]["status"] == "filled"


def test_fetch_normalized_event_models_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    event_store = SQLiteStore(db_path)
    start = datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(event_id="listing-roundtrip", event_type="listing", at=start, price=1.0, maker="0xseller", floor_price=1.0),
        _evt(event_id="sale-roundtrip", event_type="sale", at=start + timedelta(minutes=10), price=1.1, maker="0xseller", taker="0xbuyer"),
    ]
    event_store.upsert_normalized_events(events)

    models = event_store.fetch_normalized_event_models(market="okx")

    assert [model.event_id for model in models] == ["listing-roundtrip", "sale-roundtrip"]
    assert models[0].event_type == "listing"
    assert models[1].event_type == "sale"


def test_asset_analysis_triggers_relist_churn_and_oscillation_rules(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    event_store = SQLiteStore(db_path)
    fraud_store = FraudStore(db_path)
    start = datetime(2026, 3, 23, 1, 0, tzinfo=timezone.utc)
    event_store.upsert_normalized_events(
        [
            _evt(event_id="listing-a1", event_type="listing", at=start, price=1.00, maker="0xseller", floor_price=1.00),
            _evt(event_id="listing-a2", event_type="listing", at=start + timedelta(minutes=20), price=0.98, maker="0xseller", floor_price=0.98),
            _evt(event_id="listing-a3", event_type="listing", at=start + timedelta(minutes=40), price=1.18, maker="0xseller", floor_price=1.18),
        ]
    )
    materialize_from_normalized_events(event_store=event_store, fraud_store=fraud_store, market="okx")

    report = build_asset_report(fraud_store, collection_identifier="0xabc", token_id="1")
    rule_ids = {item["rule_id"] for item in report["triggered_evidence"]}

    assert "repeated_undercut_relist_near_floor" in rule_ids
    assert "repeated_cancel_relist_same_asset" in rule_ids
    assert "rapid_short_lived_listing_churn" in rule_ids
    assert "price_oscillation_same_asset_short_window" in rule_ids
    assert report["risk_assessment"]["severity"] in {"CAUTION", "HIGH_RISK", "AVOID"}


def test_collection_analysis_triggers_pair_diversity_and_cycle_rules(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    event_store = SQLiteStore(db_path)
    fraud_store = FraudStore(db_path)
    start = datetime(2026, 3, 23, 2, 0, tzinfo=timezone.utc)
    event_store.upsert_normalized_events(
        [
            _evt(event_id="sale-b1", event_type="sale", at=start, price=1.0, maker="0xa", taker="0xb"),
            _evt(event_id="sale-b2", event_type="sale", at=start + timedelta(minutes=15), price=1.1, maker="0xb", taker="0xa"),
            _evt(event_id="sale-b3", event_type="sale", at=start + timedelta(minutes=30), price=1.2, maker="0xa", taker="0xb"),
        ]
    )
    materialize_from_normalized_events(event_store=event_store, fraud_store=fraud_store, market="okx")

    report = build_collection_report(fraud_store, "0xabc")
    rule_ids = {item["rule_id"] for item in report["triggered_evidence"]}

    assert "repeated_trades_same_wallet_pair" in rule_ids
    assert "low_owner_diversity_high_volume" in rule_ids
    assert "asset_back_and_forth_transfer_pattern" in rule_ids


def test_score_caps_component_buckets() -> None:
    score = compute_risk_score(
        object_type="collection",
        object_id="col-1",
        rule_hits=[
            {
                "rule_id": "repeated_undercut_relist_near_floor",
                "severity": "high",
                "score_delta": 20.0,
                "confidence": 0.8,
                "explanation": "x",
            },
            {
                "rule_id": "floor_drop_from_small_seller_cluster",
                "severity": "high",
                "score_delta": 20.0,
                "confidence": 0.8,
                "explanation": "y",
            },
        ],
    )
    assert score["component_scores"]["floor_manipulation"] == 25.0
    assert score["total_score"] == 25.0
