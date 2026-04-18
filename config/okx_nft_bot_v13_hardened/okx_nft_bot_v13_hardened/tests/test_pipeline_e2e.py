from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import okx_nft_bot.pipeline.live_cycle as live_cycle
from okx_nft_bot.config import Settings
from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore


class CapturingNotifier(Notifier):
    channel = "capture"

    def __init__(self) -> None:
        self.sent: list[AlertEnvelope] = []

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        self.sent.append(alert)
        return DeliveryResult(
            channel=self.channel,
            event_id=alert.event.event_id,
            delivered=True,
            detail="captured",
        )


class FakeOKXClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.trade_calls: list[str | None] = []

    def get_collection_trades(
        self,
        *,
        chain: str,
        collection_address: str,
        platform: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        self.trade_calls.append(cursor)
        index = min(len(self.trade_calls) - 1, len(self.pages) - 1)
        return self.pages[index]


def _okx_trade_page(*, token_id: str, next_cursor: str | None) -> dict[str, object]:
    return {
        "data": {
            "data": [
                {
                    "txHash": "0xtx1",
                    "tokenId": token_id,
                    "timestamp": 1_710_000_001,
                    "collectionAddress": "0xabc",
                    "price": "1.5",
                    "currencyAddress": "ETH",
                    "amount": "1",
                    "from": "0xseller",
                    "to": "0xbuyer",
                }
            ],
            "cursor": next_cursor,
        }
    }


def test_registry_provider_normalization_storage_and_delivery_e2e(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "collections": [
                    {
                        "name": "alpha",
                        "market": "okx",
                        "chain": "eth",
                        "collection_address": "0xabc",
                        "enabled": True,
                        "source_modes": ["trades"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        registry_path=registry_path,
        rules_path=tmp_path / "rule_packs.json",
        active_market="okx",
        okx_max_pages_per_run=1,
    )
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(registry_path)
    notifier = CapturingNotifier()
    runner = MultiCollectionRunner(
        settings=settings,
        store=store,
        notifier=notifier,
        registry=registry,
    )
    fake_client = FakeOKXClient(
        pages=[_okx_trade_page(token_id="1", next_cursor=None)]
    )
    monkeypatch.setattr(live_cycle, "OKXMarketplaceClient", lambda settings: fake_client)

    first_run = runner.run_collection_once("alpha", source_mode="trades")

    assert first_run.target_name == "alpha"
    assert first_run.result.pages_fetched == 1
    assert len(first_run.result.raw_events) == 1
    assert len(first_run.result.new_events) == 1
    assert len(first_run.result.deliveries) == 1
    assert first_run.result.deliveries[0].delivered is True
    assert fake_client.trade_calls == [None]

    assert len(notifier.sent) == 1
    alert = notifier.sent[0]
    assert alert.event.market == "okx"
    assert alert.event.collection == "0xabc"
    assert alert.event.token_id == "1"
    assert alert.decision.passed is True

    assert store.count_events() == 1
    assert store.count_notifications() == 1
    assert store.was_notified("capture", alert.event.event_id) is True

    with sqlite3.connect(settings.db_path) as conn:
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        assert raw_count == 1
        row = conn.execute(
            "SELECT market, event_type, collection_name, token_id, tx_hash FROM normalized_events"
        ).fetchone()
        assert row == ("okx", "sale", "0xabc", "1", "0xtx1")

    second_run = runner.run_collection_once("alpha", source_mode="trades")

    assert len(second_run.result.raw_events) == 1
    assert len(second_run.result.new_events) == 0
    assert len(second_run.result.deliveries) == 0
    assert len(notifier.sent) == 1
    assert store.count_events() == 1
    assert store.count_notifications() == 1

