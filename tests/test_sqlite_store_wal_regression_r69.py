from __future__ import annotations

from okx_nft_bot.storage.sqlite import SQLiteStore


def test_sqlite_store_connections_restore_wal_and_busy_timeout(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "events.sqlite3")

    with store._connect() as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])

    assert journal_mode == "wal"
    assert busy_timeout_ms >= 10_000
