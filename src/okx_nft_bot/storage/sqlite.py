from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from okx_nft_bot.models import NFTEvent, RawEvent
from okx_nft_bot.pydantic_compat import model_dump_json_compat, model_validate_json_compat


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS normalized_events (
                    event_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    contract_address TEXT,
                    price REAL,
                    currency TEXT,
                    quantity INTEGER NOT NULL,
                    maker TEXT,
                    taker TEXT,
                    tx_hash TEXT,
                    event_time TEXT NOT NULL,
                    volume_24h REAL,
                    floor_price REAL,
                    raw_source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state_kv (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY(namespace, key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    channel TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    payload_json TEXT,
                    PRIMARY KEY(channel, event_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_normalized_events_event_time ON normalized_events(event_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_normalized_events_maker ON normalized_events(maker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_normalized_events_taker ON normalized_events(taker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_normalized_events_contract_time ON normalized_events(contract_address, event_time)")

    def insert_raw_events(self, raw_events: list[RawEvent]) -> None:
        with self._connect() as conn:
            conn.executemany(
                'INSERT INTO raw_events(source, fetched_at, payload_json) VALUES (?, ?, ?)',
                [
                    (event.source, event.fetched_at.isoformat(), json.dumps(event.payload, ensure_ascii=False))
                    for event in raw_events
                ],
            )

    def upsert_normalized_events(self, events: list[NFTEvent]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO normalized_events(
                    event_id, market, event_type, collection_name, token_id,
                    contract_address, price, currency, quantity, maker,
                    taker, tx_hash, event_time, volume_24h, floor_price,
                    raw_source, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    market=excluded.market,
                    event_type=excluded.event_type,
                    collection_name=excluded.collection_name,
                    token_id=excluded.token_id,
                    contract_address=excluded.contract_address,
                    price=excluded.price,
                    currency=excluded.currency,
                    quantity=excluded.quantity,
                    maker=excluded.maker,
                    taker=excluded.taker,
                    tx_hash=excluded.tx_hash,
                    event_time=excluded.event_time,
                    volume_24h=excluded.volume_24h,
                    floor_price=excluded.floor_price,
                    raw_source=excluded.raw_source,
                    payload_json=excluded.payload_json
                """,
                [
                    (
                        event.event_id,
                        event.market,
                        event.event_type,
                        event.collection,
                        event.token_id,
                        event.contract_address,
                        event.price,
                        event.currency,
                        event.quantity,
                        event.maker,
                        event.taker,
                        event.tx_hash,
                        event.event_time.isoformat(),
                        event.volume_24h,
                        event.floor_price,
                        event.raw_source,
                        model_dump_json_compat(event),
                    )
                    for event in events
                ],
            )

    def filter_new_events(self, events: list[NFTEvent]) -> list[NFTEvent]:
        if not events:
            return []
        event_ids = [event.event_id for event in events]
        placeholders = ','.join('?' for _ in event_ids)
        with self._connect() as conn:
            cursor = conn.execute(
                f'SELECT event_id FROM normalized_events WHERE event_id IN ({placeholders})',
                event_ids,
            )
            existing = {row[0] for row in cursor.fetchall()}
        return [event for event in events if event.event_id not in existing]

    def get_state(self, namespace: str, key: str) -> str | None:
        with self._connect() as conn:
            cursor = conn.execute(
                'SELECT value FROM state_kv WHERE namespace = ? AND key = ?',
                (namespace, key),
            )
            row = cursor.fetchone()
        return None if row is None else row[0]

    def set_state(self, namespace: str, key: str, value: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO state_kv(namespace, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value
                """,
                (namespace, key, value),
            )

    def clear_namespace(self, namespace: str) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM state_kv WHERE namespace = ?', (namespace,))

    def was_notified(self, channel: str, event_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                'SELECT 1 FROM sent_notifications WHERE channel = ? AND event_id = ?',
                (channel, event_id),
            )
            return cursor.fetchone() is not None

    def mark_notified(self, channel: str, event_id: str, payload: dict[str, object] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_notifications(channel, event_id, sent_at, payload_json)
                VALUES (?, ?, datetime('now'), ?)
                """,
                (channel, event_id, json.dumps(payload, ensure_ascii=False) if payload is not None else None),
            )

    def fetch_latest_events(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT event_id, market, event_type, collection_name, token_id, price, currency, event_time
                FROM normalized_events
                ORDER BY event_time DESC
                LIMIT ?
                """,
                (limit,),
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

    def fetch_normalized_event_models(
        self,
        *,
        limit: int | None = None,
        market: str | None = None,
    ) -> list[NFTEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if market:
            clauses.append("market = ?")
            params.append(market)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT payload_json
                FROM normalized_events
                {where}
                ORDER BY event_time ASC
                {limit_sql}
                """,
                params,
            )
            return [model_validate_json_compat(NFTEvent, row[0]) for row in cursor.fetchall()]

    def fetch_wallet_event_models(
        self,
        *,
        wallet: str,
        limit: int | None = None,
        market: str | None = None,
    ) -> list[NFTEvent]:
        resolved_wallet = wallet.strip().lower()
        if not resolved_wallet:
            return []
        clauses = ["(LOWER(COALESCE(maker, '')) = ? OR LOWER(COALESCE(taker, '')) = ?)"]
        params: list[object] = [resolved_wallet, resolved_wallet]
        if market:
            clauses.append("market = ?")
            params.append(market)
        where = f"WHERE {' AND '.join(clauses)}"
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT payload_json
                FROM normalized_events
                {where}
                ORDER BY event_time ASC
                {limit_sql}
                """,
                params,
            )
            return [model_validate_json_compat(NFTEvent, row[0]) for row in cursor.fetchall()]

    def fetch_state_rows(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            cursor = conn.execute('SELECT namespace, key, value FROM state_kv ORDER BY namespace, key')
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


    def fetch_analysis_events(self, limit: int = 5000) -> list[dict[str, object]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT event_id, market, event_type, collection_name, token_id, contract_address,
                       price, currency, quantity, maker, taker, tx_hash, event_time, volume_24h, floor_price
                FROM normalized_events
                ORDER BY event_time DESC
                LIMIT ?
                """,
                (limit,),
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

    def fetch_market_summary(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT market, collection_name, COUNT(*) AS event_count,
                       MAX(event_time) AS latest_event_time,
                       AVG(price) AS avg_price,
                       MAX(floor_price) AS floor_price
                FROM normalized_events
                GROUP BY market, collection_name
                ORDER BY market, collection_name
                """
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

    def count_events(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM normalized_events')
            return int(cursor.fetchone()[0])

    def count_notifications(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM sent_notifications')
            return int(cursor.fetchone()[0])
