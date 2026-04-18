from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3

from okx_nft_bot.storage.sqlite import SQLiteStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class BackupArtifact:
    path: Path
    created_at: datetime
    size_bytes: int
    label: str


@dataclass(slots=True)
class RestoreArtifact:
    restored_from: Path
    restored_to: Path
    restored_at: datetime
    safety_backup_path: Path | None


@dataclass(slots=True)
class ProfileStatus:
    runtime_profile: str
    desired_profile: str
    available_profiles: tuple[str, ...]


def list_profiles(profiles_dir: Path) -> tuple[str, ...]:
    if not profiles_dir.exists():
        return ('dev', 'stage', 'prod')
    names = sorted(p.stem for p in profiles_dir.glob('*.env') if p.is_file())
    return tuple(names) or ('dev', 'stage', 'prod')


def get_desired_profile(store: SQLiteStore, runtime_profile: str) -> str:
    return store.get_state('deploy_ops', 'desired_profile') or runtime_profile


def set_desired_profile(store: SQLiteStore, profile: str) -> None:
    store.set_state('deploy_ops', 'desired_profile', profile)


def read_profile_text(profiles_dir: Path, profile: str) -> str:
    path = profiles_dir / f'{profile}.env'
    return path.read_text(encoding='utf-8')


def list_backups(backup_dir: Path, limit: int | None = None) -> list[Path]:
    if not backup_dir.exists():
        return []
    items = sorted(
        (p for p in backup_dir.glob('*.sqlite3') if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if limit is not None:
        return items[: max(limit, 0)]
    return items


def _snapshot_sqlite(source_db: Path, target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_db) as src_conn, sqlite3.connect(target_db) as dst_conn:
        src_conn.backup(dst_conn)


def backup_database(db_path: Path, backup_dir: Path, *, label: str = 'manual') -> BackupArtifact:
    if not db_path.exists():
        raise FileNotFoundError(f'Database not found: {db_path}')
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime('%Y%m%dT%H%M%SZ')
    safe_label = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in label).strip('-_') or 'manual'
    target = backup_dir / f'{stamp}_{safe_label}.sqlite3'
    _snapshot_sqlite(db_path, target)
    return BackupArtifact(path=target, created_at=_utc_now(), size_bytes=target.stat().st_size, label=safe_label)


def resolve_backup_path(backup_dir: Path, name: str) -> Path:
    candidate = backup_dir / Path(name).name
    if not candidate.exists() or candidate.parent != backup_dir:
        raise FileNotFoundError(f'Backup not found: {name}')
    return candidate


def restore_database(db_path: Path, backup_path: Path, backup_dir: Path, *, create_safety_backup: bool = True) -> RestoreArtifact:
    if not backup_path.exists():
        raise FileNotFoundError(f'Backup not found: {backup_path}')
    db_path.parent.mkdir(parents=True, exist_ok=True)
    safety_backup_path: Path | None = None
    if create_safety_backup and db_path.exists():
        safety = backup_database(db_path, backup_dir, label='pre_restore')
        safety_backup_path = safety.path
    shutil.copy2(backup_path, db_path)
    return RestoreArtifact(
        restored_from=backup_path,
        restored_to=db_path,
        restored_at=_utc_now(),
        safety_backup_path=safety_backup_path,
    )
