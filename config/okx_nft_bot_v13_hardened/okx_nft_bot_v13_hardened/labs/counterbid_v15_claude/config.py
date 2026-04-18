"""
config.py  –  Parasite-Killer v15
==================================
Collection configuration manager with SQLite backing.

Tracks:
- Collection contract addresses
- Min/max price limits (BNB)
- Undercut margin (how much above parasite offer)
- Enabled/disabled state
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CollectionConfig:
    """Single collection configuration."""
    address: str          # contract address (lowercase 0x...)
    chain: str            # "bsc", etc.
    min_price_bnb: float  # minimum acceptable offer (BNB)
    max_price_bnb: float  # maximum acceptable offer (BNB)
    margin_bnb: float     # undercut margin above parasite (BNB)
    enabled: bool = True  # is this collection active?


class ConfigManager:
    """SQLite-backed collection configuration storage."""

    def __init__(self, db_path: str = "parasite_killer_v15.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    address           TEXT PRIMARY KEY,
                    chain             TEXT NOT NULL,
                    min_price_bnb     REAL NOT NULL,
                    max_price_bnb     REAL NOT NULL,
                    margin_bnb        REAL NOT NULL,
                    enabled           INTEGER NOT NULL DEFAULT 1,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def add_collection(
        self,
        address: str,
        chain: str,
        min_price_bnb: float,
        max_price_bnb: float,
        margin_bnb: float,
        enabled: bool = True,
    ) -> CollectionConfig:
        """Add or update a collection configuration."""
        address_lower = address.lower()
        if not address_lower.startswith("0x"):
            address_lower = "0x" + address_lower

        config = CollectionConfig(
            address=address_lower,
            chain=chain,
            min_price_bnb=min_price_bnb,
            max_price_bnb=max_price_bnb,
            margin_bnb=margin_bnb,
            enabled=enabled,
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO collections
                (address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    address_lower,
                    chain,
                    min_price_bnb,
                    max_price_bnb,
                    margin_bnb,
                    1 if enabled else 0,
                ),
            )
            conn.commit()

        return config

    def get_collection(self, address: str) -> Optional[CollectionConfig]:
        """Fetch a single collection by address."""
        address_lower = address.lower()
        if not address_lower.startswith("0x"):
            address_lower = "0x" + address_lower

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled FROM collections WHERE address = ?",
                (address_lower,),
            ).fetchone()

        if row:
            return CollectionConfig(
                address=row[0],
                chain=row[1],
                min_price_bnb=row[2],
                max_price_bnb=row[3],
                margin_bnb=row[4],
                enabled=bool(row[5]),
            )
        return None

    def list_collections(self, chain: Optional[str] = None, enabled_only: bool = False) -> list[CollectionConfig]:
        """List all collections, optionally filtered by chain or enabled status."""
        query = "SELECT address, chain, min_price_bnb, max_price_bnb, margin_bnb, enabled FROM collections WHERE 1=1"
        params = []

        if chain:
            query += " AND chain = ?"
            params.append(chain)

        if enabled_only:
            query += " AND enabled = 1"

        query += " ORDER BY address"

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

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
        """Remove a collection. Returns True if something was deleted."""
        address_lower = address.lower()
        if not address_lower.startswith("0x"):
            address_lower = "0x" + address_lower

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM collections WHERE address = ?",
                (address_lower,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def enable_collection(self, address: str) -> bool:
        """Enable a collection. Returns True if update succeeded."""
        address_lower = address.lower()
        if not address_lower.startswith("0x"):
            address_lower = "0x" + address_lower

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE collections SET enabled = 1, updated_at = CURRENT_TIMESTAMP WHERE address = ?",
                (address_lower,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def disable_collection(self, address: str) -> bool:
        """Disable a collection. Returns True if update succeeded."""
        address_lower = address.lower()
        if not address_lower.startswith("0x"):
            address_lower = "0x" + address_lower

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE collections SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE address = ?",
                (address_lower,),
            )
            conn.commit()
            return cursor.rowcount > 0
