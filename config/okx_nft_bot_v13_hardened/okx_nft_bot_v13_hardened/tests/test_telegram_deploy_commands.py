from pathlib import Path

from okx_nft_bot.config import load_settings
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.registry import CollectionRegistry
from okx_nft_bot.scheduler import MultiCollectionRunner
from okx_nft_bot.storage.sqlite import SQLiteStore
from okx_nft_bot.telegram_bot import TelegramCommandProcessor


class DummyClient:
    def send_message(self, *, chat_id: str, text: str):
        return {'ok': True}


class FakeRunner(MultiCollectionRunner):
    def run_collection_once(self, target_name: str, source_mode: str = 'trades'):
        raise AssertionError('should not be called')


def _make_processor(tmp_path: Path, monkeypatch) -> TelegramCommandProcessor:
    registry_path = tmp_path / 'registry.json'
    registry_path.write_text('{"collections":[{"name":"alpha","collection_address":"0x1","enabled":true,"source_modes":["trades"]}]}', encoding='utf-8')
    profiles_dir = tmp_path / 'deploy' / 'profiles'
    profiles_dir.mkdir(parents=True)
    for name in ('dev', 'stage', 'prod'):
        (profiles_dir / f'{name}.env').write_text(f'APP_PROFILE={name}\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REGISTRY_PATH', str(registry_path))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setenv('PROFILES_DIR', str(profiles_dir))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    registry = CollectionRegistry.from_path(settings.registry_path)
    runner = FakeRunner(settings=settings, store=store, notifier=NullNotifier(), registry=registry)
    return TelegramCommandProcessor(settings=settings, store=store, registry=registry, runner=runner, client=DummyClient())


def test_profile_commands(tmp_path: Path, monkeypatch) -> None:
    processor = _make_processor(tmp_path, monkeypatch)
    text = processor._handle_command(chat_id='1', text='/profiles')
    assert 'dev' in text
    set_text = processor._handle_command(chat_id='1', text='/setprofile prod')
    assert 'Desired profile set to prod' in set_text
    status = processor._handle_command(chat_id='1', text='/profile')
    assert 'desired_profile=prod' in status


def test_backup_commands(tmp_path: Path, monkeypatch) -> None:
    processor = _make_processor(tmp_path, monkeypatch)
    processor.store.set_state('x', 'y', 'z')
    backup_text = processor._handle_command(chat_id='1', text='/backup nightly')
    assert 'Backup created:' in backup_text
    list_text = processor._handle_command(chat_id='1', text='/backups 5')
    assert '.sqlite3' in list_text
