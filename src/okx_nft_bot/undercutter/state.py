from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveOffer:
    order_hash: str
    collection: str
    chain: str
    price_bnb: float
    status: str
    placed_at: datetime
    last_checked_at: datetime
    current_floor: float | None = None
    preview_payload: dict[str, Any] | None = None

    @property
    def age_hours(self) -> float:
        return max((datetime.now(timezone.utc) - self.placed_at).total_seconds() / 3600.0, 0.0)


@dataclass(slots=True)
class IntegrityAuditResult:
    checked_at: str
    issue_count: int
    quarantine_count: int
    runtime_keys_cleared: list[str]
    runtime_keys_fixed: list[str]
    offer_rows_quarantined: list[str]
    malformed_submit_rows: list[int]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return self.issue_count == 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


class PositionState:
    _ACTIVE_OFFER_ALLOWED_STATUSES = {
        "active",
        "cancelled",
        "exchange_missing",
        "killswitch_failed",
        "outbid",
        "retired",
        "state_invalid",
        "timestamp_invalid",
    }
    _BOOLEAN_TRUE = {"1", "true", "yes", "on"}
    _BOOLEAN_FALSE = {"0", "false", "no", "off"}
    _RUNTIME_DATETIME_KEYS = {
        "killswitch_activated_at",
        "last_integrity_audit_at",
        "last_reconcile_at",
        "live_armed_at",
        "live_armed_until",
        "live_disarmed_at",
    }
    _RUNTIME_NONNEGATIVE_INT_KEYS = {
        "last_integrity_issue_count",
        "last_integrity_quarantine_count",
        "last_reconcile_exchange_seen",
        "last_reconcile_local_active_seen",
        "last_reconcile_local_marked_missing",
        "last_reconcile_local_refreshed",
        "last_reconcile_local_added",
        "last_reconcile_malformed_exchange_rows",
    }

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = __import__('threading').Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_offers (
                    order_hash TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    chain TEXT NOT NULL DEFAULT 'bsc',
                    price_bnb REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    placed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    current_floor REAL,
                    preview_payload_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS undercut_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    chain TEXT NOT NULL DEFAULT 'bsc',
                    order_hash TEXT,
                    old_price_bnb REAL,
                    new_price_bnb REAL,
                    reason TEXT,
                    executed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_submit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    engine TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    chain TEXT NOT NULL DEFAULT 'bsc',
                    price_bnb REAL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_submit_log_created_at ON execution_submit_log(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_submit_log_status_created_at ON execution_submit_log(status, created_at)"
            )

    def upsert_active_offer(
        self,
        *,
        order_hash: str,
        collection: str,
        chain: str,
        price_bnb: float,
        status: str = "active",
        current_floor: float | None = None,
        preview_payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_offers (
                    order_hash, collection, chain, price_bnb, status, last_checked_at, current_floor, preview_payload_json
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                ON CONFLICT(order_hash) DO UPDATE SET
                    collection=excluded.collection,
                    chain=excluded.chain,
                    price_bnb=excluded.price_bnb,
                    status=excluded.status,
                    last_checked_at=CURRENT_TIMESTAMP,
                    current_floor=excluded.current_floor,
                    preview_payload_json=excluded.preview_payload_json
                """,
                (
                    order_hash,
                    collection.lower(),
                    chain.lower(),
                    float(price_bnb),
                    status,
                    current_floor,
                    json.dumps(preview_payload or {}, ensure_ascii=False),
                ),
            )

    def get_active_offers(self, *, chain: str | None = None) -> list[ActiveOffer]:
        return self._get_offers_by_status(status="active", chain=chain)

    def get_killswitch_failed_offers(self, *, chain: str | None = None) -> list[ActiveOffer]:
        return self._get_offers_by_status(status="killswitch_failed", chain=chain)

    def _get_offers_by_status(self, *, status: str, chain: str | None = None) -> list[ActiveOffer]:
        clauses = ["status = ?"]
        params: list[Any] = []
        params.append(status)
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT order_hash, collection, chain, price_bnb, status, placed_at, last_checked_at, current_floor, preview_payload_json
                FROM active_offers
                WHERE {where}
                ORDER BY placed_at ASC
                """,
                params,
            ).fetchall()
            offers: list[ActiveOffer] = []
            for row in rows:
                try:
                    offers.append(self._row_to_offer(row))
                except InvalidTimestampError as exc:
                    logger.warning("%s; quarantining offer row", exc)
                    conn.execute(
                        """
                        UPDATE active_offers
                        SET status = 'timestamp_invalid', last_checked_at = CURRENT_TIMESTAMP
                        WHERE order_hash = ?
                        """,
                        (str(row["order_hash"]),),
                    )
        return offers

    def list_active_offers(self, *, chain: str | None = None) -> list[dict[str, Any]]:
        return [asdict(offer) for offer in self.get_active_offers(chain=chain)]

    def mark_offer_status(
        self,
        *,
        order_hash: str,
        status: str,
        current_floor: float | None = None,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE active_offers
                SET status = ?, current_floor = COALESCE(?, current_floor), last_checked_at = CURRENT_TIMESTAMP
                WHERE order_hash = ?
                """,
                (status, current_floor, order_hash),
            )
        return cursor.rowcount > 0

    def touch_offer(self, *, order_hash: str, current_floor: float | None = None) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE active_offers
                SET current_floor = COALESCE(?, current_floor), last_checked_at = CURRENT_TIMESTAMP
                WHERE order_hash = ?
                """,
                (current_floor, order_hash),
            )
        return cursor.rowcount > 0

    def withdraw_collection(self, collection: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE active_offers
                SET status = 'cancelled', last_checked_at = CURRENT_TIMESTAMP
                WHERE collection = ? AND status = 'active'
                """,
                (collection.lower(),),
            )
        return cursor.rowcount

    def cleanup_stale_offers(self, *, max_age_days: int = 7) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT order_hash, placed_at
                FROM active_offers
                WHERE status != 'active'
                """
            ).fetchall()
            stale_hashes: list[str] = []
            for row in rows:
                try:
                    placed_at = _parse_dt(
                        str(row["placed_at"]),
                        field_name="placed_at",
                        context=f"active_offers[{row['order_hash']}]",
                    )
                except InvalidTimestampError as exc:
                    logger.warning("%s; deleting malformed non-active offer row during cleanup", exc)
                    stale_hashes.append(str(row["order_hash"]))
                    continue
                if placed_at < cutoff:
                    stale_hashes.append(str(row["order_hash"]))
            if not stale_hashes:
                return 0
            conn.executemany(
                "DELETE FROM active_offers WHERE order_hash = ?",
                [(order_hash,) for order_hash in stale_hashes],
            )
        return len(stale_hashes)

    def log_action(
        self,
        *,
        action_type: str,
        collection: str,
        chain: str,
        order_hash: str | None,
        old_price_bnb: float | None,
        new_price_bnb: float | None,
        reason: str,
        executed: bool,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        created_at = _fmt_dt(datetime.now(timezone.utc))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO undercut_log (
                    action_type, collection, chain, order_hash, old_price_bnb, new_price_bnb, reason, executed, error, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_type,
                    collection.lower(),
                    chain.lower(),
                    order_hash,
                    old_price_bnb,
                    new_price_bnb,
                    reason,
                    1 if executed else 0,
                    error,
                    json.dumps(payload or {}, ensure_ascii=False),
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_action_history(self, *, limit: int = 50, collection: str | None = None) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if collection:
            clauses.append("collection = ?")
            params.append(collection.lower())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, action_type, collection, chain, order_hash, old_price_bnb, new_price_bnb, reason, executed, error, payload_json, created_at
                FROM undercut_log
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "action_type": row["action_type"],
                "collection": row["collection"],
                "chain": row["chain"],
                "order_hash": row["order_hash"],
                "old_price_bnb": row["old_price_bnb"],
                "new_price_bnb": row["new_price_bnb"],
                "reason": row["reason"],
                "executed": bool(row["executed"]),
                "error": row["error"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def set_force_dry_run(self, enabled: bool, *, reason: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO execution_runtime_state (key, value, updated_at)
                VALUES ('force_dry_run', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                ("1" if enabled else "0",),
            )
            if reason is not None:
                conn.execute(
                    """
                    INSERT INTO execution_runtime_state (key, value, updated_at)
                    VALUES ('force_dry_run_reason', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (reason,),
                )

    def is_force_dry_run(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM execution_runtime_state WHERE key = 'force_dry_run'"
            ).fetchone()
        if row is None:
            return False
        normalized = str(row["value"]).strip().lower()
        if normalized in self._BOOLEAN_TRUE:
            return True
        if normalized in self._BOOLEAN_FALSE:
            return False
        logger.warning(
            "Invalid boolean for execution_runtime_state.force_dry_run: %r; forcing dry-run quarantine",
            row["value"],
        )
        self.set_runtime_value("force_dry_run", "1")
        self.set_runtime_value("force_dry_run_reason", "integrity_quarantine_invalid_force_dry_run")
        return True

    def effective_dry_run(self, configured_dry_run: bool) -> bool:
        return bool(configured_dry_run or self.is_force_dry_run())

    def arm_live(
        self,
        *,
        minutes: int,
        actor: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
        clear_force_dry_run: bool = True,
    ) -> dict[str, Any]:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        resolved_minutes = max(int(minutes), 1)
        expires_at = resolved_now + timedelta(minutes=resolved_minutes)
        self.set_runtime_value("live_armed_at", _fmt_dt(resolved_now))
        self.set_runtime_value("live_armed_until", _fmt_dt(expires_at))
        self.set_runtime_value("live_armed_by", actor or "unknown")
        self.set_runtime_value("live_armed_reason", reason or "")
        self.set_runtime_value("live_disarmed_at", None)
        self.set_runtime_value("live_disarmed_by", None)
        self.set_runtime_value("live_disarmed_reason", None)
        if clear_force_dry_run:
            self.set_force_dry_run(False, reason=f"live_arm:{actor or 'unknown'}")
        return self.get_live_arm_state(now=resolved_now)

    def disarm_live(
        self,
        *,
        actor: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.set_runtime_value("live_armed_until", None)
        self.set_runtime_value("live_armed_by", None)
        self.set_runtime_value("live_armed_reason", None)
        self.set_runtime_value("live_disarmed_at", _fmt_dt(resolved_now))
        self.set_runtime_value("live_disarmed_by", actor or "unknown")
        self.set_runtime_value("live_disarmed_reason", reason or "")
        return self.get_live_arm_state(now=resolved_now)

    def get_live_arm_state(self, *, now: datetime | None = None) -> dict[str, Any]:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        runtime = self.get_runtime_state()
        raw_until = runtime.get("live_armed_until")
        expires_at: datetime | None = None
        if raw_until:
            try:
                expires_at = _parse_dt(
                    str(raw_until),
                    field_name="live_armed_until",
                    context="execution_runtime_state",
                )
            except InvalidTimestampError as exc:
                logger.warning("%s; clearing malformed live arm state", exc)
                self.set_runtime_value("live_armed_until", None)
                raw_until = None
        seconds_remaining = 0
        if expires_at is not None:
            seconds_remaining = max(int((expires_at - resolved_now).total_seconds()), 0)
        armed = bool(expires_at and seconds_remaining > 0)
        return {
            "armed": armed,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "seconds_remaining": seconds_remaining,
            "minutes_remaining": seconds_remaining // 60,
            "armed_by": runtime.get("live_armed_by"),
            "reason": runtime.get("live_armed_reason"),
        }

    def get_runtime_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM execution_runtime_state ORDER BY key ASC"
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            result[row["key"]] = row["value"]
            result[f'{row["key"]}_updated_at'] = row["updated_at"]
        return result

    def audit_integrity(self, *, now: datetime | None = None) -> IntegrityAuditResult:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        runtime_keys_cleared: list[str] = []
        runtime_keys_fixed: list[str] = []
        offer_rows_quarantined: list[str] = []
        malformed_submit_rows: list[int] = []
        notes: list[str] = []

        with self._connect() as conn:
            active_rows = conn.execute(
                """
                SELECT order_hash, status, placed_at, last_checked_at
                FROM active_offers
                ORDER BY order_hash ASC
                """
            ).fetchall()
            for row in active_rows:
                order_hash = str(row["order_hash"])
                status = str(row["status"] or "").strip().lower()
                invalid_offer = False
                for field_name in ("placed_at", "last_checked_at"):
                    try:
                        _parse_dt(
                            str(row[field_name]),
                            field_name=field_name,
                            context=f"active_offers[{order_hash}]",
                        )
                    except InvalidTimestampError as exc:
                        notes.append(str(exc))
                        invalid_offer = True
                        break
                if invalid_offer:
                    conn.execute(
                        """
                        UPDATE active_offers
                        SET status = 'timestamp_invalid', last_checked_at = CURRENT_TIMESTAMP
                        WHERE order_hash = ?
                        """,
                        (order_hash,),
                    )
                    if order_hash not in offer_rows_quarantined:
                        offer_rows_quarantined.append(order_hash)
                    continue
                if status not in self._ACTIVE_OFFER_ALLOWED_STATUSES:
                    notes.append(f"Invalid status for active_offers[{order_hash}].status: {row['status']!r}")
                    conn.execute(
                        """
                        UPDATE active_offers
                        SET status = 'state_invalid', last_checked_at = CURRENT_TIMESTAMP
                        WHERE order_hash = ?
                        """,
                        (order_hash,),
                    )
                    offer_rows_quarantined.append(order_hash)

            runtime_rows = conn.execute(
                "SELECT key, value FROM execution_runtime_state ORDER BY key ASC"
            ).fetchall()
            runtime_map = {str(row["key"]): str(row["value"]) for row in runtime_rows}

            raw_force_dry_run = runtime_map.get("force_dry_run")
            if raw_force_dry_run is not None:
                normalized_force_dry_run = raw_force_dry_run.strip().lower()
                if normalized_force_dry_run in self._BOOLEAN_TRUE:
                    canonical = "1"
                elif normalized_force_dry_run in self._BOOLEAN_FALSE:
                    canonical = "0"
                else:
                    canonical = "1"
                    notes.append(f"Invalid boolean for execution_runtime_state.force_dry_run: {raw_force_dry_run!r}")
                    self._set_runtime_value_conn(
                        conn,
                        "force_dry_run_reason",
                        "integrity_quarantine_invalid_force_dry_run",
                    )
                    runtime_keys_fixed.append("force_dry_run_reason")
                if raw_force_dry_run != canonical:
                    self._set_runtime_value_conn(conn, "force_dry_run", canonical)
                    runtime_keys_fixed.append("force_dry_run")

            live_arm_keys = ("live_armed_at", "live_armed_until", "live_armed_by", "live_armed_reason")
            raw_live_armed_until = runtime_map.get("live_armed_until")
            raw_live_armed_at = runtime_map.get("live_armed_at")
            invalid_live_arm = False
            if raw_live_armed_until is not None:
                try:
                    live_armed_until = _parse_dt(
                        raw_live_armed_until,
                        field_name="live_armed_until",
                        context="execution_runtime_state",
                    )
                    if raw_live_armed_at:
                        live_armed_at = _parse_dt(
                            raw_live_armed_at,
                            field_name="live_armed_at",
                            context="execution_runtime_state",
                        )
                        if live_armed_at > live_armed_until:
                            notes.append(
                                "Invalid runtime state: execution_runtime_state.live_armed_at is after live_armed_until"
                            )
                            invalid_live_arm = True
                except InvalidTimestampError as exc:
                    notes.append(str(exc))
                    invalid_live_arm = True
            elif any(runtime_map.get(key) for key in live_arm_keys if key != "live_armed_until"):
                notes.append("Incomplete runtime state: live arm metadata present without live_armed_until")
                invalid_live_arm = True
            if invalid_live_arm:
                for key in live_arm_keys:
                    if key in runtime_map:
                        self._set_runtime_value_conn(conn, key, None)
                        runtime_keys_cleared.append(key)

            live_disarm_keys = ("live_disarmed_at", "live_disarmed_by", "live_disarmed_reason")
            raw_live_disarmed_at = runtime_map.get("live_disarmed_at")
            if raw_live_disarmed_at is not None:
                try:
                    _parse_dt(
                        raw_live_disarmed_at,
                        field_name="live_disarmed_at",
                        context="execution_runtime_state",
                    )
                except InvalidTimestampError as exc:
                    notes.append(str(exc))
                    for key in live_disarm_keys:
                        if key in runtime_map:
                            self._set_runtime_value_conn(conn, key, None)
                            runtime_keys_cleared.append(key)
            elif any(runtime_map.get(key) for key in live_disarm_keys if key != "live_disarmed_at"):
                notes.append("Incomplete runtime state: live disarm metadata present without live_disarmed_at")
                for key in live_disarm_keys:
                    if key in runtime_map:
                        self._set_runtime_value_conn(conn, key, None)
                        runtime_keys_cleared.append(key)

            for key in self._RUNTIME_DATETIME_KEYS - {"live_armed_at", "live_armed_until", "live_disarmed_at"}:
                raw_value = runtime_map.get(key)
                if raw_value is None:
                    continue
                try:
                    _parse_dt(
                        raw_value,
                        field_name=key,
                        context="execution_runtime_state",
                    )
                except InvalidTimestampError as exc:
                    notes.append(str(exc))
                    self._set_runtime_value_conn(conn, key, None)
                    runtime_keys_cleared.append(key)

            raw_reconcile_chain = runtime_map.get("last_reconcile_chain")
            if raw_reconcile_chain is not None and raw_reconcile_chain.strip().lower() != "bsc":
                notes.append(
                    f"Invalid chain for execution_runtime_state.last_reconcile_chain: {raw_reconcile_chain!r}"
                )
                self._set_runtime_value_conn(conn, "last_reconcile_chain", None)
                runtime_keys_cleared.append("last_reconcile_chain")

            for key in self._RUNTIME_NONNEGATIVE_INT_KEYS:
                raw_value = runtime_map.get(key)
                if raw_value is None:
                    continue
                try:
                    parsed = int(str(raw_value).strip())
                except ValueError:
                    notes.append(f"Invalid integer for execution_runtime_state.{key}: {raw_value!r}")
                    self._set_runtime_value_conn(conn, key, None)
                    runtime_keys_cleared.append(key)
                    continue
                if parsed < 0:
                    notes.append(f"Negative integer for execution_runtime_state.{key}: {raw_value!r}")
                    self._set_runtime_value_conn(conn, key, None)
                    runtime_keys_cleared.append(key)

            submitted_rows = conn.execute(
                """
                SELECT id, created_at
                FROM execution_submit_log
                WHERE status = 'submitted'
                ORDER BY id ASC
                """
            ).fetchall()
            for row in submitted_rows:
                try:
                    _parse_dt(
                        str(row["created_at"]),
                        field_name="created_at",
                        context=f"execution_submit_log[{row['id']}]",
                    )
                except InvalidTimestampError as exc:
                    notes.append(str(exc))
                    malformed_submit_rows.append(int(row["id"]))

        runtime_keys_cleared = list(dict.fromkeys(runtime_keys_cleared))
        runtime_keys_fixed = list(dict.fromkeys(runtime_keys_fixed))
        offer_rows_quarantined = list(dict.fromkeys(offer_rows_quarantined))
        malformed_submit_rows = list(dict.fromkeys(malformed_submit_rows))
        checked_at = _fmt_dt(resolved_now)
        result = IntegrityAuditResult(
            checked_at=checked_at,
            issue_count=len(notes),
            quarantine_count=len(runtime_keys_cleared) + len(runtime_keys_fixed) + len(offer_rows_quarantined),
            runtime_keys_cleared=runtime_keys_cleared,
            runtime_keys_fixed=runtime_keys_fixed,
            offer_rows_quarantined=offer_rows_quarantined,
            malformed_submit_rows=malformed_submit_rows,
            notes=notes,
        )
        self.set_runtime_value("last_integrity_audit_at", checked_at)
        self.set_runtime_value("last_integrity_issue_count", result.issue_count)
        self.set_runtime_value("last_integrity_quarantine_count", result.quarantine_count)
        return result

    def get_integrity_state(self) -> dict[str, Any]:
        runtime = self.get_runtime_state()
        issue_count = _coerce_nonnegative_int(runtime.get("last_integrity_issue_count"))
        quarantine_count = _coerce_nonnegative_int(runtime.get("last_integrity_quarantine_count"))
        return {
            "last_audit_at": runtime.get("last_integrity_audit_at"),
            "issue_count": issue_count or 0,
            "quarantine_count": quarantine_count or 0,
            "ok": (issue_count or 0) == 0,
        }

    def set_runtime_value(self, key: str, value: Any | None) -> None:
        with self._connect() as conn:
            self._set_runtime_value_conn(conn, key, value)

    def record_submit_event(
        self,
        *,
        engine: str,
        action_type: str,
        collection: str,
        chain: str,
        price_bnb: float | None,
        status: str,
        reason: str | None = None,
    ) -> int:
        created_at = _fmt_dt(datetime.now(timezone.utc))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO execution_submit_log (
                    engine, action_type, collection, chain, price_bnb, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    engine,
                    action_type,
                    collection.lower(),
                    chain.lower(),
                    price_bnb,
                    status,
                    reason,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def count_successful_live_submits_since(self, since: datetime) -> int:
        resolved_since = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return sum(1 for _, created_at in self._iter_valid_submitted_rows() if created_at >= resolved_since)

    def sum_successful_live_bnb_since(self, since: datetime) -> float:
        resolved_since = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return sum(
            float(row["price_bnb"] or 0.0)
            for row, created_at in self._iter_valid_submitted_rows()
            if created_at >= resolved_since
        )

    def latest_successful_live_submit_at(self) -> datetime | None:
        rows = self._iter_valid_submitted_rows()
        return rows[0][1] if rows else None

    def get_rate_limit_snapshot(
        self,
        *,
        now: datetime,
        max_live_offers_per_hour: int,
        max_bnb_per_day: float,
        submit_cooldown_seconds: int,
    ) -> dict[str, Any]:
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)
        hourly_count = self.count_successful_live_submits_since(hour_ago)
        daily_bnb = self.sum_successful_live_bnb_since(day_ago)
        last_submit_at = self.latest_successful_live_submit_at()
        cooldown_remaining = 0
        if last_submit_at is not None:
            elapsed = max((now - last_submit_at).total_seconds(), 0.0)
            cooldown_remaining = max(int(submit_cooldown_seconds - elapsed), 0)
        return {
            "hourly_count": hourly_count,
            "hourly_limit": max_live_offers_per_hour,
            "daily_bnb": daily_bnb,
            "daily_limit_bnb": max_bnb_per_day,
            "cooldown_seconds": submit_cooldown_seconds,
            "cooldown_remaining_seconds": cooldown_remaining,
            "last_submit_at": last_submit_at,
        }

    def get_today_action_summary(
        self,
        *,
        now: datetime | None = None,
        chain: str | None = None,
    ) -> dict[str, Any]:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        target_date = resolved_now.date().isoformat()
        clauses = ["date(created_at) = date(?)"]
        params: list[Any] = [target_date]
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            action_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN action_type LIKE '%ATTACK' THEN 1 ELSE 0 END) AS attacks,
                    SUM(CASE WHEN action_type LIKE '%WITHDRAW' THEN 1 ELSE 0 END) AS withdraws
                FROM undercut_log
                WHERE {where}
                """,
                params,
            ).fetchone()
        day_start = datetime.combine(resolved_now.date(), datetime.min.time(), tzinfo=timezone.utc)
        next_day = day_start + timedelta(days=1)
        bnb_spent = 0.0
        for row, created_at in self._iter_valid_submitted_rows(chain=chain):
            if day_start <= created_at < next_day:
                bnb_spent += float(row["price_bnb"] or 0.0)
        return {
            "total": int(action_row["total"] or 0) if action_row is not None else 0,
            "attacks": int(action_row["attacks"] or 0) if action_row is not None else 0,
            "withdraws": int(action_row["withdraws"] or 0) if action_row is not None else 0,
            "bnb_spent": bnb_spent,
        }

    def get_today_submit_count(
        self,
        *,
        now: datetime | None = None,
        chain: str | None = None,
    ) -> int:
        """Count today's submissions from execution_submit_log (more reliable than undercut_log)."""
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        target_date = resolved_now.date().isoformat()
        clauses = ["date(created_at) = date(?)", "status = 'submitted'"]
        params: list[Any] = [target_date]
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM execution_submit_log WHERE {where}",
                params,
            ).fetchone()
        return int(row["cnt"] or 0) if row else 0

    def get_hourly_submit_count(
        self,
        *,
        now: datetime | None = None,
        chain: str | None = None,
    ) -> int:
        resolved_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        hour_ago = resolved_now - timedelta(hours=1)
        return sum(
            1
            for _, created_at in self._iter_valid_submitted_rows(chain=chain)
            if created_at >= hour_ago
        )

    def get_tracked_collections_count(self, *, chain: str | None = None) -> int:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if chain:
            clauses.append("chain = ?")
            params.append(chain.lower())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT collection) AS count FROM active_offers WHERE {where}",
                params,
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def _row_to_offer(self, row: sqlite3.Row) -> ActiveOffer:
        return ActiveOffer(
            order_hash=row["order_hash"],
            collection=row["collection"],
            chain=row["chain"],
            price_bnb=float(row["price_bnb"]),
            status=row["status"],
            placed_at=_parse_dt(
                str(row["placed_at"]),
                field_name="placed_at",
                context=f"active_offers[{row['order_hash']}]",
            ),
            last_checked_at=_parse_dt(
                str(row["last_checked_at"]),
                field_name="last_checked_at",
                context=f"active_offers[{row['order_hash']}]",
            ),
            current_floor=float(row["current_floor"]) if row["current_floor"] is not None else None,
            preview_payload=json.loads(row["preview_payload_json"] or "{}"),
        )

    def _iter_valid_submitted_rows(
        self,
        *,
        chain: str | None = None,
    ) -> list[tuple[sqlite3.Row, datetime]]:
        params: list[Any] = ["submitted"]
        chain_clause = ""
        if chain:
            chain_clause = " AND chain = ?"
            params.append(chain.lower())
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, engine, action_type, collection, chain, price_bnb, status, reason, created_at
                FROM execution_submit_log
                WHERE status = ?{chain_clause}
                ORDER BY id DESC
                """,
                params,
            ).fetchall()
        valid_rows: list[tuple[sqlite3.Row, datetime]] = []
        for row in rows:
            try:
                created_at = _parse_dt(
                    str(row["created_at"]),
                    field_name="created_at",
                    context=f"execution_submit_log[{row['id']}]",
                )
            except InvalidTimestampError as exc:
                logger.warning("%s; ignoring malformed submit-log row", exc)
                continue
            valid_rows.append((row, created_at))
        return valid_rows

    @staticmethod
    def _set_runtime_value_conn(conn: sqlite3.Connection, key: str, value: Any | None) -> None:
        if value is None:
            conn.execute("DELETE FROM execution_runtime_state WHERE key = ?", (key,))
            return
        conn.execute(
            """
            INSERT INTO execution_runtime_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )


class InvalidTimestampError(ValueError):
    def __init__(self, *, value: str, field_name: str, context: str) -> None:
        self.value = value
        self.field_name = field_name
        self.context = context
        super().__init__(f"Invalid timestamp for {context}.{field_name}: {value!r}")


def _parse_dt(value: str, *, field_name: str, context: str) -> datetime:
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise InvalidTimestampError(value=value, field_name=field_name, context=context)


def _fmt_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _coerce_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed
