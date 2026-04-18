from dataclasses import replace
from pathlib import Path

from okx_nft_bot.config import load_settings
from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.notifiers.null import NullNotifier


class FakeRunner(MultiCollectionRunner):
    def run_collection_once(self, target_name: str, source_mode: str = "trades"):
        class Result:
            pages_fetched = 1
            new_events = [1, 2]
            deliveries = [DeliveryResult(channel="null", event_id="x", delivered=True)]
        class Wrapper:
            def __init__(self):
                self.target_name = target_name
                self.source_mode = source_mode
                self.result = Result()
        return Wrapper()


def test_run_all_once_dispatches_registry(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"collections":[{"name":"a","collection_address":"0x1","enabled":true,"source_modes":["trades","listings"]},{"name":"b","collection_address":"0x2","enabled":true,"source_modes":["trades"]}]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))
    settings = load_settings()
    registry = CollectionRegistry.from_path(settings.registry_path)
    store = SQLiteStore(settings.db_path)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    results = runner.run_all_once()
    assert [(item.target_name, item.source_mode) for item in results] == [
        ("a", "trades"),
        ("a", "listings"),
        ("b", "trades"),
    ]


def test_run_daemon_emits_health_alert_checks_when_channels_configured(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"collections":[{"name":"a","collection_address":"0x1","enabled":true,"source_modes":["trades"]}]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    settings = load_settings()
    registry = CollectionRegistry.from_path(settings.registry_path)
    store = SQLiteStore(settings.db_path)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    calls: list[str] = []
    monkeypatch.setattr("okx_nft_bot.scheduler.maybe_send_health_alert", lambda settings, *, store, source: calls.append(source))

    summary = runner.run_daemon(interval_seconds=0, max_cycles=1)

    assert summary.cycles == 1
    assert calls == ["main_daemon", "main_daemon"]
