from pathlib import Path

from okx_nft_bot.config import load_settings
from okx_nft_bot.deploy_ops import backup_database, list_backups, resolve_backup_path, restore_database
from okx_nft_bot.storage.sqlite import SQLiteStore


def test_profile_overlay_is_loaded(tmp_path: Path, monkeypatch) -> None:
    profiles_dir = tmp_path / 'deploy' / 'profiles'
    profiles_dir.mkdir(parents=True)
    (profiles_dir / 'stage.env').write_text('SCHEDULER_INTERVAL_SECONDS=123\nAPP_ENV=stage\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('APP_PROFILE', 'stage')
    monkeypatch.setenv('PROFILES_DIR', str(profiles_dir))
    settings = load_settings()
    assert settings.app_profile == 'stage'
    assert settings.scheduler_interval_seconds == 123
    assert settings.app_env == 'stage'


def test_backup_and_restore_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path / 'backups'))
    settings = load_settings()
    store = SQLiteStore(settings.db_path)
    store.set_state('demo', 'value', 'alpha')

    backup = backup_database(settings.db_path, settings.backup_dir, label='roundtrip')
    assert backup.path.exists()
    assert len(list_backups(settings.backup_dir)) == 1

    store.set_state('demo', 'value', 'beta')
    restore_database(settings.db_path, resolve_backup_path(settings.backup_dir, backup.path.name), settings.backup_dir)

    restored_store = SQLiteStore(settings.db_path)
    assert restored_store.get_state('demo', 'value') == 'alpha'
    backups = list_backups(settings.backup_dir)
    assert any('pre_restore' in item.name for item in backups)
