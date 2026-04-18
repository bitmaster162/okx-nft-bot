from pathlib import Path
from urllib.parse import parse_qs

from okx_nft_bot.config import load_settings
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramBotClient, TelegramCommandProcessor
from okx_nft_bot.notifiers.null import NullNotifier


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def request_json(self, *, method, url, headers, body=""):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if url.endswith('/getUpdates'):
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 11,
                        "message": {
                            "chat": {"id": 123},
                            "text": "/collections",
                        },
                    }
                ],
            }
        return {"ok": True, "result": {"message_id": 1}}


class FakeRunner(MultiCollectionRunner):
    def run_collection_once(self, target_name: str, source_mode: str = "trades"):
        raise AssertionError("should not be called")


def test_poll_once_processes_collection_command(tmp_path: Path, monkeypatch) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"collections":[{"name":"alpha","collection_address":"0x1","enabled":true,"source_modes":["trades"]}]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "123")
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(settings.registry_path)
    fake_transport = FakeTransport()
    client = TelegramBotClient(bot_token="token", transport=fake_transport)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    processor = TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=client)
    result = processor.poll_once()
    assert result["processed"] == 1
    assert store.get_state("telegram_bot", "update_offset") == "12"
    send_call = fake_transport.calls[-1]
    payload = parse_qs(send_call["body"])
    assert "Active collections" in payload["text"][0]
