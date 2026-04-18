from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REMOVE_PATTERNS = [
    '.env',
    '.idea',
    '.pytest_cache',
    '.claude',
    'config/okx_cookies.json',
    'data/backups',
    'data/corrupted_backup',
    'data/corrupted_backup_20260403',
]
GLOB_REMOVE = [
    '**/desktop.ini',
    '**/__pycache__',
    '**/*.pyc',
    'data/**/*.sqlite3',
    'data/**/*.sqlite3-*',
    'data/**/*.log',
    'data/**/*.log.*',
    'data/**/*.png',
    'data/*snapshot*',
    'data/project_id_cache.json',
]


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def sanitize_repo(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    for relative in REMOVE_PATTERNS:
        _remove_path(dst / relative)

    for pattern in GLOB_REMOVE:
        for path in dst.glob(pattern):
            _remove_path(path)

    data_dir = dst / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / '.gitkeep').touch()

    env_example = dst / '.env.example'
    if env_example.exists():
        (dst / '.env.redacted').write_text(env_example.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description='Create a sanitized release copy of the repo.')
    parser.add_argument('src', type=Path)
    parser.add_argument('dst', type=Path)
    args = parser.parse_args()
    sanitize_repo(args.src, args.dst)


if __name__ == '__main__':
    main()
