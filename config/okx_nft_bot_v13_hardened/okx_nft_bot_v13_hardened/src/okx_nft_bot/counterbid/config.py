from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class CollectionConfig:
    address: str
    chain: str
    min_price_bnb: float
    max_price_bnb: float
    margin_bnb: float
    enabled: bool = True


class CounterbidConfigManager:
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
                CREATE TABLE IF NOT EXISTS counterbid_collections (
                    address TEXT PRIMARY KEY,
                    chain TEXT NOT NULL,
                    min_price_bnb REAL NOT NULL,
                    max_price_bnb REAL NOT NULL,
                    margin_bnb REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _normalize_address(address: str) -> str:
        value = address.strip().lower()
        return value if value.startswith("0x") else f"0x{value}"

    def add_collection(
        self,
        *,
        address: str,
        chain: str,
        min_price_bnb: float,
        max_price_bnb: float,
        margin_bnb: float,
        enabled: bool = True,
    ) -> CollectionConfig:
        config = CollectionConfig(
            address=self._normalize_address(address),
            chain=chain.lower(),
            min_price_bnb=min_price_bnb,
            max_price_bnb=max_price_bnb,
            margin_bnb=margin_bnb,
            enabled=enabled,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO counterbid_collections(
                    address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(address) DO UPDATE SET
                    chain=excluded.chain,
                    min_price_bnb=excluded.min_price_bnb,
                    max_price_bnb=excluded.max_price_bnb,
                    margin_bnb=excluded.margin_bnb,
                    enabled=excluded.enabled,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    config.address,
                    config.chain,
                    config.min_price_bnb,
                    config.max_price_bnb,
                    config.margin_bnb,
                    1 if config.enabled else 0,
                ),
            )
        return config

    def get_collection(self, address: str) -> CollectionConfig | None:
        normalized = self._normalize_address(address)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled
                FROM counterbid_collections
                WHERE address = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return CollectionConfig(
            address=row[0],
            chain=row[1],
            min_price_bnb=row[2],
            max_price_bnb=row[3],
            margin_bnb=row[4],
            enabled=bool(row[5]),
        )

    def list_collections(self, *, chain: str | None = None, enabled_only: bool = False) -> list[CollectionConfig]:
        clauses: list[str] = ["1=1"]
        params: list[object] = []
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        if enabled_only:
            clauses.append("enabled = 1")
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled
                FROM counterbid_collections
                WHERE {where}
                ORDER BY address
                """,
                params,
            ).fetchall()
        return [
            CollectionConfig(
                address=row[0],
                chain=row[1],
                min_price_bnb=row[2],
                max_price_bnb=row[3],
                margin_bnb=row[4],
                enabled=bool(row[5]),
            )
            for row in rows
        ]

    def remove_collection(self, address: str) -> bool:
        normalized = self._normalize_address(address)
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM counterbid_collections WHERE address = ?", (normalized,))
        return cursor.rowcount > 0

    def set_enabled(self, address: str, enabled: bool) -> bool:
        normalized = self._normalize_address(address)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE counterbid_collections
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE address = ?
                """,
                (1 if enabled else 0, normalized),
            )
        return cursor.rowcount > 0

    def enable_collection(self, address: str) -> bool:
        return self.set_enabled(address, True)

    def disable_collection(self, address: str) -> bool:
        return self.set_enabled(address, False)

    def describe(self) -> list[dict[str, object]]:
        return [asdict(config) for config in self.list_collections()]
