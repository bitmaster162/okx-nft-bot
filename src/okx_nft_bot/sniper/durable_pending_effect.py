from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class DurablePendingEffectStore:
    """Durable fail-closed claims for ambiguous instant-buy effects.

    Claims are keyed by ``(wallet, chain, order_id)`` in the existing execution
    SQLite database. They are intentionally conservative: a surviving claim
    means the prior process may have crossed the external effect boundary, so a
    later process must not submit the same order again without reconciliation.
    Completed claims are terminal tombstones and cannot be released for retry.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
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
                WHERE wallet=? AND chain=? AND order_id=? AND state!='completed'
                """,
                (str(tx_hash or "").strip() or None, *identity),
            )

    def release(self, *, wallet: str, chain: str, order_id: str) -> None:
        identity = self._identity(wallet, chain, order_id)
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM instant_buy_pending_effects
                WHERE wallet=? AND chain=? AND order_id=? AND state!='completed'
                """,
                identity,
            )

    def fetch_claims(
        self,
        *,
        wallet: str | None = None,
        chain: str | None = None,
        order_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if wallet is not None:
            clauses.append("wallet=?")
            params.append(str(wallet).strip().lower())
        if chain is not None:
            resolved_chain = str(chain).strip().lower()
            if resolved_chain == "ethereum":
                resolved_chain = "eth"
            clauses.append("chain=?")
            params.append(resolved_chain)
        if order_id is not None:
            clauses.append("order_id=?")
            params.append(str(order_id).strip())
        if state is not None:
            clauses.append("state=?")
            params.append(str(state).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT wallet, chain, order_id, state, tx_hash, created_at, updated_at
                FROM instant_buy_pending_effects
                {where}
                ORDER BY updated_at ASC, wallet ASC, chain ASC, order_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_claim(
        self,
        *,
        wallet: str,
        chain: str,
        order_id: str,
        resolution: str,
    ) -> bool:
        identity = self._identity(wallet, chain, order_id)
        resolved_resolution = str(resolution or "").strip().lower()
        if resolved_resolution not in {"mark-completed", "release-for-retry"}:
            raise ValueError(
                "resolution must be 'mark-completed' or 'release-for-retry'"
            )

        with self._connect() as conn:
            if resolved_resolution == "release-for-retry":
                cursor = conn.execute(
                    """
                    DELETE FROM instant_buy_pending_effects
                    WHERE wallet=? AND chain=? AND order_id=? AND state='pending'
                    """,
                    identity,
                )
                return cursor.rowcount == 1

            cursor = conn.execute(
                """
                UPDATE instant_buy_pending_effects
                SET state='completed', updated_at=CURRENT_TIMESTAMP
                WHERE wallet=? AND chain=? AND order_id=?
                """,
                identity,
            )
            return cursor.rowcount == 1
