from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_nft_bot.normalizers.offers import NormalizedOffer


@dataclass(slots=True)
class OfferFilters:
    market: str | None = None
    collection: str | None = None
    chain: str | None = None
    maker: str | None = None
    status: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    active_only: bool = False
    limit: int = 50


class OffersStore:
    """SQLite-backed store for normalized NFT offers. Read-optimized; no order submission paths."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offers (
                    offer_id      TEXT PRIMARY KEY,
                    market        TEXT NOT NULL,
                    chain         TEXT NOT NULL,
                    collection    TEXT NOT NULL,
                    token_id      TEXT,
                    maker         TEXT,
                    price         REAL,
                    currency      TEXT,
                    quantity      INTEGER NOT NULL DEFAULT 1,
                    status        TEXT NOT NULL DEFAULT 'active',
                    created_at    TEXT,
                    expires_at    TEXT,
                    source_type   TEXT NOT NULL,
                    source_reliability TEXT NOT NULL,
                    raw_payload_hash TEXT NOT NULL,
                    observed_at   TEXT NOT NULL,
                    payload_json  TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_market ON offers(market)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_collection ON offers(collection)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_maker ON offers(maker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_observed ON offers(observed_at DESC)")

    def upsert_offers(self, offers: list[NormalizedOffer]) -> int:
        if not offers:
            return 0
        rows = [_offer_to_row(o) for o in offers]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO offers(
                    offer_id, market, chain, collection, token_id,
                    maker, price, currency, quantity, status,
                    created_at, expires_at, source_type, source_reliability,
                    raw_payload_hash, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id) DO UPDATE SET
                    status=excluded.status,
                    price=excluded.price,
                    expires_at=excluded.expires_at,
                    raw_payload_hash=excluded.raw_payload_hash,
                    observed_at=excluded.observed_at,
                    payload_json=excluded.payload_json
                """,
                rows,
            )
        return len(rows)

    def query_offers(self, filters: OfferFilters) -> list[NormalizedOffer]:
        clauses: list[str] = []
        params: list[Any] = []

        if filters.market:
            clauses.append("market = ?")
            params.append(filters.market.lower())
        if filters.collection:
            clauses.append("collection LIKE ?")
            params.append(f"%{filters.collection.lower()}%")
        if filters.chain:
            clauses.append("chain = ?")
            params.append(filters.chain.lower())
        if filters.maker:
            clauses.append("maker = ?")
            params.append(filters.maker.lower())
        if filters.status:
            clauses.append("status = ?")
            params.append(filters.status)
        elif filters.active_only:
            clauses.append("status = 'active'")
        if filters.min_price is not None:
            clauses.append("price >= ?")
            params.append(filters.min_price)
        if filters.max_price is not None:
            clauses.append("price <= ?")
            params.append(filters.max_price)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT offer_id, market, chain, collection, token_id,
                   maker, price, currency, quantity, status,
                   created_at, expires_at, source_type, source_reliability,
                   raw_payload_hash, observed_at
            FROM offers
            {where}
            ORDER BY observed_at DESC
            LIMIT ?
        """
        params.append(filters.limit)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            return [_row_to_offer(dict(zip(columns, row, strict=False))) for row in cursor.fetchall()]

    def count_offers(self, *, market: str | None = None, status: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if market:
            clauses.append("market = ?")
            params.append(market.lower())
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            cursor = conn.execute(f"SELECT COUNT(*) FROM offers {where}", params)
            return int(cursor.fetchone()[0])


def _offer_to_row(offer: NormalizedOffer) -> tuple[Any, ...]:
    return (
        offer.offer_id,
        offer.market,
        offer.chain,
        offer.collection_slug_or_address,
        offer.token_id,
        offer.maker,
        offer.price,
        offer.currency,
        offer.quantity,
        offer.status,
        offer.created_at.isoformat() if offer.created_at else None,
        offer.expires_at.isoformat() if offer.expires_at else None,
        offer.source_type,
        offer.source_reliability,
        offer.raw_payload_hash,
        offer.observed_at.isoformat(),
        offer.model_dump_json(),
    )


def _row_to_offer(row: dict[str, Any]) -> NormalizedOffer:
    return NormalizedOffer(
        offer_id=row["offer_id"],
        market=row["market"],
        chain=row["chain"],
        collection_slug_or_address=row["collection"],
        token_id=row.get("token_id"),
        maker=row.get("maker"),
        price=row.get("price"),
        currency=row.get("currency"),
        quantity=int(row.get("quantity") or 1),
        status=str(row.get("status") or "active"),
        created_at=_parse_dt(row.get("created_at")),
        expires_at=_parse_dt(row.get("expires_at")),
        source_type=row["source_type"],
        source_reliability=row["source_reliability"],
        raw_payload_hash=row["raw_payload_hash"],
        observed_at=_parse_dt(row["observed_at"]) or datetime.now(timezone.utc),
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
