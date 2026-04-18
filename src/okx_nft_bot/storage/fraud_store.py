from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


class FraudStore:
    """Canonical NFT fraud schema on top of the existing monitor SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    external_collection_id TEXT,
                    slug TEXT,
                    name TEXT NOT NULL,
                    contract_address TEXT,
                    owner_count INTEGER,
                    item_count INTEGER,
                    listing_count INTEGER,
                    floor_price_native REAL,
                    floor_price_usd REAL,
                    volume_24h_native REAL,
                    volume_7d_native REAL,
                    volume_all_native REAL,
                    source_url TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_collections_marketplace ON collections(marketplace);
                CREATE INDEX IF NOT EXISTS idx_collections_contract ON collections(contract_address);
                CREATE INDEX IF NOT EXISTS idx_collections_name ON collections(name);

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    external_asset_id TEXT,
                    collection_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    contract_address TEXT,
                    metadata_uri TEXT,
                    attributes_json TEXT,
                    current_owner_entity_id TEXT,
                    current_listing_id TEXT,
                    current_listing_price_native REAL,
                    current_listing_price_usd REAL,
                    last_sale_price_native REAL,
                    last_sale_price_usd REAL,
                    listing_status TEXT,
                    source_url TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(collection_id) REFERENCES collections(id)
                );
                CREATE INDEX IF NOT EXISTS idx_assets_collection ON assets(collection_id);
                CREATE INDEX IF NOT EXISTS idx_assets_token ON assets(token_id);
                CREATE INDEX IF NOT EXISTS idx_assets_owner ON assets(current_owner_entity_id);

                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    external_listing_id TEXT,
                    source_event_id TEXT,
                    collection_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    seller_entity_id TEXT,
                    listed_price_native REAL,
                    listed_price_usd REAL,
                    currency_symbol TEXT,
                    listed_at TEXT,
                    delisted_at TEXT,
                    status TEXT NOT NULL,
                    source_url TEXT,
                    raw_ref_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(collection_id) REFERENCES collections(id),
                    FOREIGN KEY(asset_id) REFERENCES assets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_listings_collection ON listings(collection_id);
                CREATE INDEX IF NOT EXISTS idx_listings_asset ON listings(asset_id);
                CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_entity_id);
                CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
                CREATE INDEX IF NOT EXISTS idx_listings_listed_at ON listings(listed_at);

                CREATE TABLE IF NOT EXISTS sales (
                    id TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    external_sale_id TEXT,
                    tx_hash TEXT,
                    source_event_id TEXT,
                    collection_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    seller_entity_id TEXT,
                    buyer_entity_id TEXT,
                    sale_price_native REAL,
                    sale_price_usd REAL,
                    currency_symbol TEXT,
                    sale_timestamp TEXT NOT NULL,
                    source_url TEXT,
                    raw_ref_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(collection_id) REFERENCES collections(id),
                    FOREIGN KEY(asset_id) REFERENCES assets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sales_collection ON sales(collection_id);
                CREATE INDEX IF NOT EXISTS idx_sales_asset ON sales(asset_id);
                CREATE INDEX IF NOT EXISTS idx_sales_pair ON sales(seller_entity_id, buyer_entity_id);
                CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(sale_timestamp);

                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    external_entity_id TEXT,
                    wallet_address TEXT,
                    display_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    role_flags_json TEXT NOT NULL,
                    suspicious_score REAL NOT NULL DEFAULT 0,
                    linked_entities_count INTEGER NOT NULL DEFAULT 0,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_entities_wallet ON entities(wallet_address);
                CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

                CREATE TABLE IF NOT EXISTS entity_links (
                    id TEXT PRIMARY KEY,
                    src_entity_id TEXT NOT NULL,
                    dst_entity_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    notes TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_links_unique ON entity_links(src_entity_id, dst_entity_id, link_type);

                CREATE TABLE IF NOT EXISTS floor_snapshots (
                    id TEXT PRIMARY KEY,
                    collection_id TEXT NOT NULL,
                    snapshot_ts TEXT NOT NULL,
                    floor_price_native REAL,
                    floor_price_usd REAL,
                    listing_count INTEGER,
                    sample_size INTEGER,
                    source_url TEXT,
                    raw_ref_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_floor_snapshots_collection ON floor_snapshots(collection_id);
                CREATE INDEX IF NOT EXISTS idx_floor_snapshots_ts ON floor_snapshots(snapshot_ts);

                CREATE TABLE IF NOT EXISTS rule_hits (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    score_delta REAL NOT NULL,
                    triggered_at TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    explanation TEXT NOT NULL,
                    evidence_bundle_id TEXT,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rule_hits_object ON rule_hits(object_type, object_id);
                CREATE INDEX IF NOT EXISTS idx_rule_hits_rule ON rule_hits(rule_id);

                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    analyst_note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_object ON evidence(object_type, object_id);

                CREATE TABLE IF NOT EXISTS risk_scores (
                    id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    total_score REAL NOT NULL,
                    severity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    component_scores_json TEXT NOT NULL,
                    top_rules_json TEXT NOT NULL,
                    explanation_json TEXT NOT NULL,
                    scored_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_scores_unique ON risk_scores(object_type, object_id);

                CREATE TABLE IF NOT EXISTS watchlist (
                    id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_unique ON watchlist(object_type, object_id);
                CREATE INDEX IF NOT EXISTS idx_watchlist_status ON watchlist(status);

                CREATE TABLE IF NOT EXISTS analyst_notes (
                    id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    note_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    verdict TEXT,
                    created_at TEXT NOT NULL,
                    author TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_analyst_notes_object ON analyst_notes(object_type, object_id);
                """
            )

    def upsert_collection(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO collections(
                id, marketplace, chain, external_collection_id, slug, name,
                contract_address, owner_count, item_count, listing_count,
                floor_price_native, floor_price_usd, volume_24h_native,
                volume_7d_native, volume_all_native, source_url,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                marketplace=excluded.marketplace,
                chain=excluded.chain,
                external_collection_id=COALESCE(excluded.external_collection_id, collections.external_collection_id),
                slug=COALESCE(excluded.slug, collections.slug),
                name=COALESCE(excluded.name, collections.name),
                contract_address=COALESCE(excluded.contract_address, collections.contract_address),
                owner_count=COALESCE(excluded.owner_count, collections.owner_count),
                item_count=COALESCE(excluded.item_count, collections.item_count),
                listing_count=COALESCE(excluded.listing_count, collections.listing_count),
                floor_price_native=COALESCE(excluded.floor_price_native, collections.floor_price_native),
                floor_price_usd=COALESCE(excluded.floor_price_usd, collections.floor_price_usd),
                volume_24h_native=COALESCE(excluded.volume_24h_native, collections.volume_24h_native),
                volume_7d_native=COALESCE(excluded.volume_7d_native, collections.volume_7d_native),
                volume_all_native=COALESCE(excluded.volume_all_native, collections.volume_all_native),
                source_url=COALESCE(excluded.source_url, collections.source_url),
                first_seen_at=min(collections.first_seen_at, excluded.first_seen_at),
                last_seen_at=max(collections.last_seen_at, excluded.last_seen_at),
                updated_at=excluded.updated_at
            """,
            (
                record["id"],
                record["marketplace"],
                record["chain"],
                record.get("external_collection_id"),
                record.get("slug"),
                record["name"],
                record.get("contract_address"),
                record.get("owner_count"),
                record.get("item_count"),
                record.get("listing_count"),
                record.get("floor_price_native"),
                record.get("floor_price_usd"),
                record.get("volume_24h_native"),
                record.get("volume_7d_native"),
                record.get("volume_all_native"),
                record.get("source_url"),
                record["first_seen_at"],
                record["last_seen_at"],
                record["updated_at"],
            ),
        )

    def upsert_asset(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO assets(
                id, marketplace, chain, external_asset_id, collection_id, token_id,
                contract_address, metadata_uri, attributes_json, current_owner_entity_id,
                current_listing_id, current_listing_price_native, current_listing_price_usd,
                last_sale_price_native, last_sale_price_usd, listing_status, source_url,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                marketplace=excluded.marketplace,
                chain=excluded.chain,
                external_asset_id=COALESCE(excluded.external_asset_id, assets.external_asset_id),
                collection_id=excluded.collection_id,
                token_id=excluded.token_id,
                contract_address=COALESCE(excluded.contract_address, assets.contract_address),
                metadata_uri=COALESCE(excluded.metadata_uri, assets.metadata_uri),
                attributes_json=COALESCE(excluded.attributes_json, assets.attributes_json),
                current_owner_entity_id=COALESCE(excluded.current_owner_entity_id, assets.current_owner_entity_id),
                current_listing_id=COALESCE(excluded.current_listing_id, assets.current_listing_id),
                current_listing_price_native=COALESCE(excluded.current_listing_price_native, assets.current_listing_price_native),
                current_listing_price_usd=COALESCE(excluded.current_listing_price_usd, assets.current_listing_price_usd),
                last_sale_price_native=COALESCE(excluded.last_sale_price_native, assets.last_sale_price_native),
                last_sale_price_usd=COALESCE(excluded.last_sale_price_usd, assets.last_sale_price_usd),
                listing_status=COALESCE(excluded.listing_status, assets.listing_status),
                source_url=COALESCE(excluded.source_url, assets.source_url),
                first_seen_at=min(assets.first_seen_at, excluded.first_seen_at),
                last_seen_at=max(assets.last_seen_at, excluded.last_seen_at),
                updated_at=excluded.updated_at
            """,
            (
                record["id"],
                record["marketplace"],
                record["chain"],
                record.get("external_asset_id"),
                record["collection_id"],
                record["token_id"],
                record.get("contract_address"),
                record.get("metadata_uri"),
                _json_dumps(record.get("attributes_json", {})),
                record.get("current_owner_entity_id"),
                record.get("current_listing_id"),
                record.get("current_listing_price_native"),
                record.get("current_listing_price_usd"),
                record.get("last_sale_price_native"),
                record.get("last_sale_price_usd"),
                record.get("listing_status"),
                record.get("source_url"),
                record["first_seen_at"],
                record["last_seen_at"],
                record["updated_at"],
            ),
        )

    def upsert_entity(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        existing = conn.execute(
            "SELECT role_flags_json FROM entities WHERE id = ?",
            (record["id"],),
        ).fetchone()
        roles = set(record.get("role_flags_json", []) or [])
        if existing and existing["role_flags_json"]:
            roles.update(json.loads(existing["role_flags_json"]))

        conn.execute(
            """
            INSERT INTO entities(
                id, marketplace, chain, entity_type, external_entity_id, wallet_address,
                display_name, first_seen_at, last_seen_at, role_flags_json,
                suspicious_score, linked_entities_count, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                marketplace=excluded.marketplace,
                chain=excluded.chain,
                entity_type=excluded.entity_type,
                external_entity_id=COALESCE(excluded.external_entity_id, entities.external_entity_id),
                wallet_address=COALESCE(excluded.wallet_address, entities.wallet_address),
                display_name=COALESCE(excluded.display_name, entities.display_name),
                first_seen_at=min(entities.first_seen_at, excluded.first_seen_at),
                last_seen_at=max(entities.last_seen_at, excluded.last_seen_at),
                role_flags_json=excluded.role_flags_json,
                notes=COALESCE(excluded.notes, entities.notes)
            """,
            (
                record["id"],
                record["marketplace"],
                record["chain"],
                record.get("entity_type", "wallet"),
                record.get("external_entity_id"),
                record.get("wallet_address"),
                record.get("display_name"),
                record["first_seen_at"],
                record["last_seen_at"],
                _json_dumps(sorted(roles)),
                record.get("suspicious_score", 0.0),
                record.get("linked_entities_count", 0),
                record.get("notes"),
            ),
        )

    def close_active_listings_for_asset(
        self,
        conn: sqlite3.Connection,
        *,
        asset_id: str,
        closed_at: str,
        status: str,
        exclude_listing_id: str | None = None,
    ) -> None:
        if exclude_listing_id:
            conn.execute(
                """
                UPDATE listings
                SET status = ?, delisted_at = COALESCE(delisted_at, ?), updated_at = ?
                WHERE asset_id = ? AND status = 'active' AND id != ?
                """,
                (status, closed_at, closed_at, asset_id, exclude_listing_id),
            )
            return
        conn.execute(
            """
            UPDATE listings
            SET status = ?, delisted_at = COALESCE(delisted_at, ?), updated_at = ?
            WHERE asset_id = ? AND status = 'active'
            """,
            (status, closed_at, closed_at, asset_id),
        )

    def upsert_listing(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO listings(
                id, marketplace, chain, external_listing_id, source_event_id,
                collection_id, asset_id, seller_entity_id, listed_price_native,
                listed_price_usd, currency_symbol, listed_at, delisted_at,
                status, source_url, raw_ref_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                marketplace=excluded.marketplace,
                chain=excluded.chain,
                external_listing_id=COALESCE(excluded.external_listing_id, listings.external_listing_id),
                source_event_id=COALESCE(excluded.source_event_id, listings.source_event_id),
                collection_id=excluded.collection_id,
                asset_id=excluded.asset_id,
                seller_entity_id=COALESCE(excluded.seller_entity_id, listings.seller_entity_id),
                listed_price_native=COALESCE(excluded.listed_price_native, listings.listed_price_native),
                listed_price_usd=COALESCE(excluded.listed_price_usd, listings.listed_price_usd),
                currency_symbol=COALESCE(excluded.currency_symbol, listings.currency_symbol),
                listed_at=COALESCE(excluded.listed_at, listings.listed_at),
                delisted_at=COALESCE(excluded.delisted_at, listings.delisted_at),
                status=excluded.status,
                source_url=COALESCE(excluded.source_url, listings.source_url),
                raw_ref_id=COALESCE(excluded.raw_ref_id, listings.raw_ref_id),
                updated_at=excluded.updated_at
            """,
            (
                record["id"],
                record["marketplace"],
                record["chain"],
                record.get("external_listing_id"),
                record.get("source_event_id"),
                record["collection_id"],
                record["asset_id"],
                record.get("seller_entity_id"),
                record.get("listed_price_native"),
                record.get("listed_price_usd"),
                record.get("currency_symbol"),
                record.get("listed_at"),
                record.get("delisted_at"),
                record["status"],
                record.get("source_url"),
                record.get("raw_ref_id"),
                record["created_at"],
                record["updated_at"],
            ),
        )

    def upsert_sale(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO sales(
                id, marketplace, chain, external_sale_id, tx_hash, source_event_id,
                collection_id, asset_id, seller_entity_id, buyer_entity_id,
                sale_price_native, sale_price_usd, currency_symbol, sale_timestamp,
                source_url, raw_ref_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                marketplace=excluded.marketplace,
                chain=excluded.chain,
                external_sale_id=COALESCE(excluded.external_sale_id, sales.external_sale_id),
                tx_hash=COALESCE(excluded.tx_hash, sales.tx_hash),
                source_event_id=COALESCE(excluded.source_event_id, sales.source_event_id),
                collection_id=excluded.collection_id,
                asset_id=excluded.asset_id,
                seller_entity_id=COALESCE(excluded.seller_entity_id, sales.seller_entity_id),
                buyer_entity_id=COALESCE(excluded.buyer_entity_id, sales.buyer_entity_id),
                sale_price_native=COALESCE(excluded.sale_price_native, sales.sale_price_native),
                sale_price_usd=COALESCE(excluded.sale_price_usd, sales.sale_price_usd),
                currency_symbol=COALESCE(excluded.currency_symbol, sales.currency_symbol),
                sale_timestamp=excluded.sale_timestamp,
                source_url=COALESCE(excluded.source_url, sales.source_url),
                raw_ref_id=COALESCE(excluded.raw_ref_id, sales.raw_ref_id),
                created_at=excluded.created_at
            """,
            (
                record["id"],
                record["marketplace"],
                record["chain"],
                record.get("external_sale_id"),
                record.get("tx_hash"),
                record.get("source_event_id"),
                record["collection_id"],
                record["asset_id"],
                record.get("seller_entity_id"),
                record.get("buyer_entity_id"),
                record.get("sale_price_native"),
                record.get("sale_price_usd"),
                record.get("currency_symbol"),
                record["sale_timestamp"],
                record.get("source_url"),
                record.get("raw_ref_id"),
                record["created_at"],
            ),
        )

    def insert_floor_snapshot(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO floor_snapshots(
                id, collection_id, snapshot_ts, floor_price_native, floor_price_usd,
                listing_count, sample_size, source_url, raw_ref_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["collection_id"],
                record["snapshot_ts"],
                record.get("floor_price_native"),
                record.get("floor_price_usd"),
                record.get("listing_count"),
                record.get("sample_size"),
                record.get("source_url"),
                record.get("raw_ref_id"),
            ),
        )

    def rebuild_asset_state(self, conn: sqlite3.Connection) -> None:
        asset_ids = [row["id"] for row in conn.execute("SELECT id FROM assets").fetchall()]
        for asset_id in asset_ids:
            latest_listing = conn.execute(
                """
                SELECT id, listed_price_native
                FROM listings
                WHERE asset_id = ? AND status = 'active'
                ORDER BY listed_at DESC, created_at DESC
                LIMIT 1
                """,
                (asset_id,),
            ).fetchone()
            latest_sale = conn.execute(
                """
                SELECT buyer_entity_id, sale_price_native
                FROM sales
                WHERE asset_id = ?
                ORDER BY sale_timestamp DESC, created_at DESC
                LIMIT 1
                """,
                (asset_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE assets
                SET current_listing_id = ?,
                    current_listing_price_native = ?,
                    last_sale_price_native = COALESCE(?, last_sale_price_native),
                    current_owner_entity_id = COALESCE(?, current_owner_entity_id),
                    listing_status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    latest_listing["id"] if latest_listing else None,
                    latest_listing["listed_price_native"] if latest_listing else None,
                    latest_sale["sale_price_native"] if latest_sale else None,
                    latest_sale["buyer_entity_id"] if latest_sale else None,
                    "listed" if latest_listing else "unlisted",
                    _utcnow_iso(),
                    asset_id,
                ),
            )

    def rebuild_collection_aggregates(self, conn: sqlite3.Connection) -> None:
        collection_ids = [row["id"] for row in conn.execute("SELECT id FROM collections").fetchall()]
        now_iso = _utcnow_iso()
        for collection_id in collection_ids:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM assets WHERE collection_id = ?) AS item_count,
                    (SELECT COUNT(DISTINCT current_owner_entity_id) FROM assets WHERE collection_id = ? AND current_owner_entity_id IS NOT NULL) AS owner_count,
                    (SELECT COUNT(*) FROM listings WHERE collection_id = ? AND status = 'active') AS listing_count,
                    (SELECT MIN(listed_price_native) FROM listings WHERE collection_id = ? AND status = 'active') AS floor_price_native,
                    (SELECT COALESCE(SUM(sale_price_native), 0) FROM sales WHERE collection_id = ? AND datetime(sale_timestamp) >= datetime('now', '-1 day')) AS volume_24h_native,
                    (SELECT COALESCE(SUM(sale_price_native), 0) FROM sales WHERE collection_id = ? AND datetime(sale_timestamp) >= datetime('now', '-7 day')) AS volume_7d_native,
                    (SELECT COALESCE(SUM(sale_price_native), 0) FROM sales WHERE collection_id = ?) AS volume_all_native
                """,
                (collection_id, collection_id, collection_id, collection_id, collection_id, collection_id, collection_id),
            ).fetchone()
            conn.execute(
                """
                UPDATE collections
                SET item_count = ?,
                    owner_count = ?,
                    listing_count = ?,
                    floor_price_native = COALESCE(?, floor_price_native),
                    volume_24h_native = ?,
                    volume_7d_native = ?,
                    volume_all_native = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    row["item_count"],
                    row["owner_count"],
                    row["listing_count"],
                    row["floor_price_native"],
                    row["volume_24h_native"],
                    row["volume_7d_native"],
                    row["volume_all_native"],
                    now_iso,
                    collection_id,
                ),
            )

    def rebuild_entity_links(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM entity_links")
        rows = conn.execute(
            """
            SELECT seller_entity_id, buyer_entity_id,
                   COUNT(*) AS edge_count,
                   MIN(sale_timestamp) AS first_seen_at,
                   MAX(sale_timestamp) AS last_seen_at
            FROM sales
            WHERE seller_entity_id IS NOT NULL AND buyer_entity_id IS NOT NULL
            GROUP BY seller_entity_id, buyer_entity_id
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO entity_links(
                    id, src_entity_id, dst_entity_id, link_type,
                    weight, evidence_count, first_seen_at, last_seen_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"link:{row['seller_entity_id']}:{row['buyer_entity_id']}:trade_counterparty",
                    row["seller_entity_id"],
                    row["buyer_entity_id"],
                    "trade_counterparty",
                    float(row["edge_count"]),
                    int(row["edge_count"]),
                    row["first_seen_at"],
                    row["last_seen_at"],
                    None,
                ),
            )

    def rebuild_entity_metrics(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE entities SET linked_entities_count = 0")
        conn.execute(
            """
            UPDATE entities
            SET linked_entities_count = (
                SELECT COUNT(*)
                FROM entity_links
                WHERE src_entity_id = entities.id OR dst_entity_id = entities.id
            )
            """
        )

    def clear_analysis(self, conn: sqlite3.Connection, *, object_type: str, object_id: str) -> None:
        conn.execute("DELETE FROM rule_hits WHERE object_type = ? AND object_id = ?", (object_type, object_id))
        conn.execute("DELETE FROM evidence WHERE object_type = ? AND object_id = ?", (object_type, object_id))
        conn.execute("DELETE FROM risk_scores WHERE object_type = ? AND object_id = ?", (object_type, object_id))

    def store_analysis(
        self,
        conn: sqlite3.Connection,
        *,
        object_type: str,
        object_id: str,
        rule_hits: list[dict[str, Any]],
        risk_score: dict[str, Any],
    ) -> None:
        self.clear_analysis(conn, object_type=object_type, object_id=object_id)
        for hit in rule_hits:
            evidence_id = f"evidence:{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO evidence(
                    id, object_type, object_id, evidence_type, payload_json,
                    source_refs_json, created_at, confidence, analyst_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    object_type,
                    object_id,
                    hit["evidence_type"],
                    _json_dumps(hit["evidence_payload"]),
                    _json_dumps(hit["source_refs"]),
                    hit["triggered_at"],
                    float(hit["confidence"]),
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO rule_hits(
                    id, rule_id, object_type, object_id, severity, score_delta,
                    triggered_at, window_start, window_end, explanation,
                    evidence_bundle_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rulehit:{uuid4().hex}",
                    hit["rule_id"],
                    object_type,
                    object_id,
                    hit["severity"],
                    float(hit["score_delta"]),
                    hit["triggered_at"],
                    hit.get("window_start"),
                    hit.get("window_end"),
                    hit["explanation"],
                    evidence_id,
                    "active",
                ),
            )

        conn.execute(
            """
            INSERT INTO risk_scores(
                id, object_type, object_id, total_score, severity, confidence,
                component_scores_json, top_rules_json, explanation_json, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_type, object_id) DO UPDATE SET
                total_score=excluded.total_score,
                severity=excluded.severity,
                confidence=excluded.confidence,
                component_scores_json=excluded.component_scores_json,
                top_rules_json=excluded.top_rules_json,
                explanation_json=excluded.explanation_json,
                scored_at=excluded.scored_at
            """,
            (
                f"risk:{uuid4().hex}",
                object_type,
                object_id,
                float(risk_score["total_score"]),
                risk_score["severity"],
                float(risk_score["confidence"]),
                _json_dumps(risk_score["component_scores"]),
                _json_dumps(risk_score["top_rules"]),
                _json_dumps(risk_score["explanation"]),
                risk_score["scored_at"],
            ),
        )

    def add_watchlist_item(
        self,
        *,
        object_type: str,
        object_id: str,
        reason: str,
        priority: str,
        status: str = "active",
    ) -> dict[str, Any]:
        added_at = _utcnow_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, object_type, object_id, added_at, reason, priority, status FROM watchlist WHERE object_type = ? AND object_id = ?",
                (object_type, object_id),
            ).fetchone()
            watch_id = existing["id"] if existing else f"watch:{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO watchlist(id, object_type, object_id, added_at, reason, priority, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_type, object_id) DO UPDATE SET
                    reason=excluded.reason,
                    priority=excluded.priority,
                    status=excluded.status
                """,
                (watch_id, object_type, object_id, added_at, reason, priority, status),
            )
            row = conn.execute(
                "SELECT id, object_type, object_id, added_at, reason, priority, status FROM watchlist WHERE object_type = ? AND object_id = ?",
                (object_type, object_id),
            ).fetchone()
            return dict(row)

    def list_watchlist(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE w.status = ?"
            params.append(status)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    w.id,
                    w.object_type,
                    w.object_id,
                    w.added_at,
                    w.reason,
                    w.priority,
                    w.status,
                    r.total_score,
                    r.severity AS risk_severity,
                    r.confidence AS risk_confidence,
                    r.scored_at
                FROM watchlist w
                LEFT JOIN risk_scores r
                  ON r.object_type = w.object_type
                 AND r.object_id = w.object_id
                {where}
                ORDER BY
                    CASE w.priority
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    w.added_at DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_collection(self, identifier: str) -> dict[str, Any] | None:
        query = """
            SELECT * FROM collections
            WHERE id = ?
               OR lower(contract_address) = lower(?)
               OR name = ? COLLATE NOCASE
               OR external_collection_id = ?
               OR slug = ? COLLATE NOCASE
            ORDER BY updated_at DESC
            LIMIT 1
        """
        with self.connect() as conn:
            row = conn.execute(query, (identifier, identifier, identifier, identifier, identifier)).fetchone()
            return None if row is None else dict(row)

    def resolve_asset(
        self,
        *,
        asset_id: str | None = None,
        collection_identifier: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            if asset_id:
                row = conn.execute("SELECT * FROM assets WHERE id = ? LIMIT 1", (asset_id,)).fetchone()
                return None if row is None else dict(row)
            if collection_identifier and token_id:
                collection = self.resolve_collection(collection_identifier)
                if not collection:
                    return None
                row = conn.execute(
                    "SELECT * FROM assets WHERE collection_id = ? AND token_id = ? LIMIT 1",
                    (collection["id"], token_id),
                ).fetchone()
                return None if row is None else dict(row)
        return None

    def resolve_entity(self, identifier: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM entities
                WHERE id = ?
                   OR lower(wallet_address) = lower(?)
                   OR display_name = ? COLLATE NOCASE
                   OR external_entity_id = ?
                LIMIT 1
                """,
                (identifier, identifier, identifier, identifier),
            ).fetchone()
            return None if row is None else dict(row)

    def fetch_collection_assets(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM assets WHERE collection_id = ? ORDER BY token_id", (collection_id,)).fetchall()]

    def fetch_collection_listings(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM listings WHERE collection_id = ? ORDER BY listed_at", (collection_id,)).fetchall()]

    def fetch_collection_sales(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM sales WHERE collection_id = ? ORDER BY sale_timestamp", (collection_id,)).fetchall()]

    def fetch_floor_snapshots(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM floor_snapshots WHERE collection_id = ? ORDER BY snapshot_ts", (collection_id,)).fetchall()]

    def fetch_asset_listings(self, asset_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM listings WHERE asset_id = ? ORDER BY listed_at", (asset_id,)).fetchall()]

    def fetch_asset_sales(self, asset_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM sales WHERE asset_id = ? ORDER BY sale_timestamp", (asset_id,)).fetchall()]

    def fetch_entity_listings(self, entity_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM listings WHERE seller_entity_id = ? ORDER BY listed_at", (entity_id,)).fetchall()]

    def fetch_entity_sales(self, entity_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM sales
                    WHERE seller_entity_id = ? OR buyer_entity_id = ?
                    ORDER BY sale_timestamp
                    """,
                    (entity_id, entity_id),
                ).fetchall()
            ]

    def fetch_entity_links(self, entity_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM entity_links WHERE src_entity_id = ? OR dst_entity_id = ? ORDER BY weight DESC",
                    (entity_id, entity_id),
                ).fetchall()
            ]

    def fetch_rule_hits(self, *, object_type: str, object_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM rule_hits WHERE object_type = ? AND object_id = ? ORDER BY score_delta DESC, triggered_at DESC",
                    (object_type, object_id),
                ).fetchall()
            ]

    def fetch_risk_score(self, *, object_type: str, object_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM risk_scores WHERE object_type = ? AND object_id = ? LIMIT 1",
                (object_type, object_id),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["component_scores_json"] = json.loads(payload["component_scores_json"])
            payload["top_rules_json"] = json.loads(payload["top_rules_json"])
            payload["explanation_json"] = json.loads(payload["explanation_json"])
            return payload

    def table_counts(self) -> dict[str, int]:
        tables = [
            "collections",
            "assets",
            "listings",
            "sales",
            "entities",
            "entity_links",
            "floor_snapshots",
            "rule_hits",
            "evidence",
            "risk_scores",
            "watchlist",
            "analyst_notes",
        ]
        with self.connect() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def describe_object(self, *, object_type: str, object_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            if object_type == "collection":
                row = conn.execute("SELECT id, name, contract_address, marketplace FROM collections WHERE id = ?", (object_id,)).fetchone()
                return {} if row is None else dict(row)
            if object_type == "asset":
                row = conn.execute(
                    """
                    SELECT a.id, a.token_id, a.collection_id, c.name AS collection_name, c.marketplace
                    FROM assets a
                    JOIN collections c ON c.id = a.collection_id
                    WHERE a.id = ?
                    """,
                    (object_id,),
                ).fetchone()
                return {} if row is None else dict(row)
            row = conn.execute(
                "SELECT id, wallet_address, display_name, marketplace FROM entities WHERE id = ?",
                (object_id,),
            ).fetchone()
            return {} if row is None else dict(row)
