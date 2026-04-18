from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MassOfferCampaign:
    campaign_id: int
    collection: str
    chain: str
    price_bnb: float
    duration_hours: int
    delay_seconds: float
    dry_run: bool
    status: str
    rarity_filter: tuple[str, ...]
    unlisted_only: bool
    exclude_own: bool
    max_existing_offer: float | None
    min_token_id: int | None
    max_token_id: int | None
    max_total: int
    scanned_count: int
    target_count: int
    submitted_count: int
    dry_run_count: int
    skipped_count: int
    failed_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class MassOfferRecord:
    record_id: int
    campaign_id: int
    collection: str
    chain: str
    token_id: int
    owner: str | None
    rarity: str | None
    listed: bool
    existing_offer_bnb: float | None
    price_bnb: float
    status: str
    reason: str | None
    offer_ref: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MassOfferTracker:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mass_offer_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    price_bnb REAL NOT NULL,
                    duration_hours INTEGER NOT NULL,
                    delay_seconds REAL NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'running',
                    rarity_filter TEXT,
                    unlisted_only INTEGER NOT NULL DEFAULT 0,
                    exclude_own INTEGER NOT NULL DEFAULT 1,
                    max_existing_offer REAL,
                    min_token_id INTEGER,
                    max_token_id INTEGER,
                    max_total INTEGER NOT NULL,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    submitted_count INTEGER NOT NULL DEFAULT 0,
                    dry_run_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mass_offer_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    collection TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    token_id INTEGER NOT NULL,
                    owner TEXT,
                    rarity TEXT,
                    listed INTEGER NOT NULL DEFAULT 0,
                    existing_offer_bnb REAL,
                    price_bnb REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    offer_ref TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(campaign_id, token_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mass_offer_items_status ON mass_offer_items(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mass_offer_items_campaign ON mass_offer_items(campaign_id)")

    def start_campaign(
        self,
        *,
        collection: str,
        chain: str,
        price_bnb: float,
        duration_hours: int,
        delay_seconds: float,
        dry_run: bool,
        rarity_filter: tuple[str, ...],
        unlisted_only: bool,
        exclude_own: bool,
        max_existing_offer: float | None,
        min_token_id: int | None,
        max_token_id: int | None,
        max_total: int,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO mass_offer_campaigns (
                    collection, chain, price_bnb, duration_hours, delay_seconds, dry_run, status,
                    rarity_filter, unlisted_only, exclude_own, max_existing_offer, min_token_id, max_token_id, max_total
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collection.lower(),
                    chain.lower(),
                    float(price_bnb),
                    int(duration_hours),
                    float(delay_seconds),
                    1 if dry_run else 0,
                    ",".join(rarity_filter),
                    1 if unlisted_only else 0,
                    1 if exclude_own else 0,
                    max_existing_offer,
                    min_token_id,
                    max_token_id,
                    int(max_total),
                ),
            )
            return int(cursor.lastrowid)

    def record_item(
        self,
        *,
        campaign_id: int,
        collection: str,
        chain: str,
        token_id: int,
        owner: str | None,
        rarity: str | None,
        listed: bool,
        existing_offer_bnb: float | None,
        price_bnb: float,
        status: str,
        reason: str | None = None,
        offer_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mass_offer_items (
                    campaign_id, collection, chain, token_id, owner, rarity, listed, existing_offer_bnb,
                    price_bnb, status, reason, offer_ref, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(campaign_id, token_id) DO UPDATE SET
                    owner=excluded.owner,
                    rarity=excluded.rarity,
                    listed=excluded.listed,
                    existing_offer_bnb=excluded.existing_offer_bnb,
                    price_bnb=excluded.price_bnb,
                    status=excluded.status,
                    reason=excluded.reason,
                    offer_ref=excluded.offer_ref,
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    campaign_id,
                    collection.lower(),
                    chain.lower(),
                    int(token_id),
                    owner.lower() if owner else None,
                    rarity,
                    1 if listed else 0,
                    existing_offer_bnb,
                    float(price_bnb),
                    status,
                    reason,
                    offer_ref,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )

    def complete_campaign(
        self,
        *,
        campaign_id: int,
        status: str,
        scanned_count: int,
        target_count: int,
        submitted_count: int,
        dry_run_count: int,
        skipped_count: int,
        failed_count: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mass_offer_campaigns
                SET status = ?,
                    scanned_count = ?,
                    target_count = ?,
                    submitted_count = ?,
                    dry_run_count = ?,
                    skipped_count = ?,
                    failed_count = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    int(scanned_count),
                    int(target_count),
                    int(submitted_count),
                    int(dry_run_count),
                    int(skipped_count),
                    int(failed_count),
                    campaign_id,
                ),
            )

    def mark_item_status(self, *, record_id: int, status: str, reason: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mass_offer_items
                SET status = ?, reason = COALESCE(?, reason), updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, reason, record_id),
            )

    def mark_campaign_status(self, *, campaign_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mass_offer_campaigns
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, campaign_id),
            )

    def list_campaigns(self, *, limit: int = 10) -> list[MassOfferCampaign]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM mass_offer_campaigns
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_campaign(row) for row in rows]

    def list_active_records(
        self,
        *,
        chain: str | None = None,
        collection: str | None = None,
        limit: int = 200,
    ) -> list[MassOfferRecord]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        if collection:
            clauses.append("collection = ?")
            params.append(collection.lower())
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM mass_offer_items
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_campaign(row: sqlite3.Row) -> MassOfferCampaign:
        return MassOfferCampaign(
            campaign_id=int(row["id"]),
            collection=str(row["collection"]),
            chain=str(row["chain"]),
            price_bnb=float(row["price_bnb"]),
            duration_hours=int(row["duration_hours"]),
            delay_seconds=float(row["delay_seconds"]),
            dry_run=bool(row["dry_run"]),
            status=str(row["status"]),
            rarity_filter=tuple(part for part in str(row["rarity_filter"] or "").split(",") if part),
            unlisted_only=bool(row["unlisted_only"]),
            exclude_own=bool(row["exclude_own"]),
            max_existing_offer=float(row["max_existing_offer"]) if row["max_existing_offer"] is not None else None,
            min_token_id=int(row["min_token_id"]) if row["min_token_id"] is not None else None,
            max_token_id=int(row["max_token_id"]) if row["max_token_id"] is not None else None,
            max_total=int(row["max_total"]),
            scanned_count=int(row["scanned_count"]),
            target_count=int(row["target_count"]),
            submitted_count=int(row["submitted_count"]),
            dry_run_count=int(row["dry_run_count"]),
            skipped_count=int(row["skipped_count"]),
            failed_count=int(row["failed_count"]),
            created_at=_parse_dt(str(row["created_at"])),
            updated_at=_parse_dt(str(row["updated_at"])),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MassOfferRecord:
        return MassOfferRecord(
            record_id=int(row["id"]),
            campaign_id=int(row["campaign_id"]),
            collection=str(row["collection"]),
            chain=str(row["chain"]),
            token_id=int(row["token_id"]),
            owner=row["owner"],
            rarity=row["rarity"],
            listed=bool(row["listed"]),
            existing_offer_bnb=float(row["existing_offer_bnb"]) if row["existing_offer_bnb"] is not None else None,
            price_bnb=float(row["price_bnb"]),
            status=str(row["status"]),
            reason=row["reason"],
            offer_ref=row["offer_ref"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=_parse_dt(str(row["created_at"])),
            updated_at=_parse_dt(str(row["updated_at"])),
        )


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
