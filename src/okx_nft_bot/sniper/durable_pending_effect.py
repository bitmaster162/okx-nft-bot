from __future__ import annotations

import sqlite3
from pathlib import Path


class DurablePendingEffectStore:
    """Durable fail-closed claims for ambiguous instant-buy effects.

    Claims are keyed by ``(wallet, chain, order_id)`` in the existing execution
    SQLite database. They are intentionally conservative: a surviving claim
    means the prior process may have crossed the external effect boundary, so a
    later process must not submit the same order again without reconciliation.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instant_buy_pending_effects (
                    wallet TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'reserved',
                    tx_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(wallet, chain, order_id)
                )
                """
            )

    @staticmethod
    def _identity(wallet: str, chain: str, order_id: str) -> tuple[str, str, str]:
        resolved_wallet = str(wallet or "").strip().lower()
        resolved_chain = str(chain or "").strip().lower()
        if resolved_chain == "ethereum":
            resolved_chain = "eth"
        resolved_order_id = str(order_id or "").strip()
        if not resolved_wallet or not resolved_chain or not resolved_order_id:
            raise ValueError("wallet, chain and order_id are required for durable effect identity")
        return resolved_wallet, resolved_chain, resolved_order_id

    def reserve(self, *, wallet: str, chain: str, order_id: str) -> bool:
        identity = self._identity(wallet, chain, order_id)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO instant_buy_pending_effects (
                    wallet, chain, order_id, state, tx_hash, updated_at
                ) VALUES (?, ?, ?, 'reserved', NULL, CURRENT_TIMESTAMP)
                """,
                identity,
            )
            return cursor.rowcount == 1

    def mark_pending(
        self,
        *,
        wallet: str,
        chain: str,
        order_id: str,
        tx_hash: str | None = None,
    ) -> None:
        identity = self._identity(wallet, chain, order_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE instant_buy_pending_effects
                SET state='pending', tx_hash=?, updated_at=CURRENT_TIMESTAMP
                WHERE wallet=? AND chain=? AND order_id=?
                """,
                (str(tx_hash or "").strip() or None, *identity),
            )

    def release(self, *, wallet: str, chain: str, order_id: str) -> None:
        identity = self._identity(wallet, chain, order_id)
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM instant_buy_pending_effects
                WHERE wallet=? AND chain=? AND order_id=?
                """,
                identity,
            )
