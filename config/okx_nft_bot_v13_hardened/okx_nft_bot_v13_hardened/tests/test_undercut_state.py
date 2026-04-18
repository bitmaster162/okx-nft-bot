from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from okx_nft_bot.undercutter.state import PositionState


def test_position_state_roundtrip_and_withdraw(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")
    state.upsert_active_offer(
        order_hash="dryrun-1",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.5,
        preview_payload={"signature": "0xabc"},
    )
    offers = state.get_active_offers(chain="bsc")
    assert len(offers) == 1
    assert offers[0].collection == "0xabc"
    assert offers[0].preview_payload == {"signature": "0xabc"}

    state.touch_offer(order_hash="dryrun-1", current_floor=0.45)
    refreshed = state.get_active_offers(chain="bsc")[0]
    assert refreshed.current_floor == 0.45

    assert state.withdraw_collection("0xabc") == 1
    assert state.get_active_offers(chain="bsc") == []


def test_position_state_logs_actions(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")
    row_id = state.log_action(
        action_type="ATTACK",
        collection="0xabc",
        chain="bsc",
        order_hash="dryrun-1",
        old_price_bnb=None,
        new_price_bnb=0.5,
        reason="No offers",
        executed=True,
        payload={"foo": "bar"},
    )
    history = state.list_action_history(limit=5)
    assert row_id > 0
    assert len(history) == 1
    assert history[0]["action_type"] == "ATTACK"
    assert history[0]["payload"] == {"foo": "bar"}


def test_position_state_supports_stale_offer_updates(tmp_path: Path) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    state.upsert_active_offer(
        order_hash="dryrun-1",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.5,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE active_offers SET placed_at = '2020-01-01T00:00:00+00:00' WHERE order_hash = 'dryrun-1'")
    stale = state.get_active_offers(chain="bsc")[0]
    assert stale.age_hours > 24


def test_position_state_force_dry_run_override(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")
    assert state.effective_dry_run(False) is False
    state.set_force_dry_run(True, reason="test_killswitch")
    assert state.is_force_dry_run() is True
    assert state.effective_dry_run(False) is True
    runtime = state.get_runtime_state()
    assert runtime["force_dry_run"] == "1"
    assert runtime["force_dry_run_reason"] == "test_killswitch"


def test_position_state_invalid_force_dry_run_fails_closed(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO execution_runtime_state (key, value, updated_at)
            VALUES ('force_dry_run', 'maybe', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """
        )

    with caplog.at_level("WARNING"):
        assert state.is_force_dry_run() is True

    runtime = state.get_runtime_state()
    assert runtime["force_dry_run"] == "1"
    assert runtime["force_dry_run_reason"] == "integrity_quarantine_invalid_force_dry_run"
    assert "Invalid boolean for execution_runtime_state.force_dry_run" in caplog.text


def test_position_state_live_arm_window_roundtrip(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")

    armed = state.arm_live(minutes=15, actor="test", reason="unit")

    assert armed["armed"] is True
    assert armed["minutes_remaining"] <= 15
    assert armed["armed_by"] == "test"
    assert armed["reason"] == "unit"
    assert state.is_force_dry_run() is False

    disarmed = state.disarm_live(actor="test", reason="done")

    assert disarmed["armed"] is False


def test_position_state_tracks_live_submit_limits(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")
    state.record_submit_event(
        engine="counterbid",
        action_type="LIVE_COUNTERBID",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.4,
        status="submitted",
        reason="offer_id=1",
    )
    now = datetime.now(timezone.utc) + timedelta(seconds=5)
    snapshot = state.get_rate_limit_snapshot(
        now=now,
        max_live_offers_per_hour=10,
        max_bnb_per_day=5.0,
        submit_cooldown_seconds=30,
    )
    assert snapshot["hourly_count"] == 1
    assert snapshot["daily_bnb"] == 0.4
    assert snapshot["cooldown_remaining_seconds"] > 0


def test_position_state_cleanup_stale_offers_removes_only_old_non_active_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    state.upsert_active_offer(order_hash="active-1", collection="0xabc", chain="bsc", price_bnb=0.5, status="active")
    state.upsert_active_offer(order_hash="cancelled-old", collection="0xabc", chain="bsc", price_bnb=0.4, status="cancelled")
    state.upsert_active_offer(order_hash="outbid-old", collection="0xabc", chain="bsc", price_bnb=0.45, status="outbid")
    state.upsert_active_offer(order_hash="cancelled-recent", collection="0xabc", chain="bsc", price_bnb=0.35, status="cancelled")

    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    recent_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE active_offers SET placed_at = ? WHERE order_hash = 'cancelled-old'", (old_timestamp,))
        conn.execute("UPDATE active_offers SET placed_at = ? WHERE order_hash = 'outbid-old'", (old_timestamp,))
        conn.execute("UPDATE active_offers SET placed_at = ? WHERE order_hash = 'cancelled-recent'", (recent_timestamp,))
        conn.execute("UPDATE active_offers SET placed_at = ? WHERE order_hash = 'active-1'", (old_timestamp,))

    cleaned = state.cleanup_stale_offers(max_age_days=7)

    assert cleaned == 2
    active = state.get_active_offers(chain="bsc")
    assert [offer.order_hash for offer in active] == ["active-1"]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT order_hash, status FROM active_offers ORDER BY order_hash").fetchall()
    assert {(row[0], row[1]) for row in rows} == {
        ("active-1", "active"),
        ("cancelled-recent", "cancelled"),
    }


def test_position_state_quarantines_offer_with_invalid_timestamp(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    state.upsert_active_offer(order_hash="broken-offer", collection="0xabc", chain="bsc", price_bnb=0.5)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE active_offers SET placed_at = 'not-a-timestamp' WHERE order_hash = 'broken-offer'")

    with caplog.at_level("WARNING"):
        offers = state.get_active_offers(chain="bsc")

    assert offers == []
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM active_offers WHERE order_hash = 'broken-offer'").fetchone()
    assert row is not None
    assert row[0] == "timestamp_invalid"
    assert "Invalid timestamp for active_offers[broken-offer].placed_at" in caplog.text


def test_position_state_ignores_malformed_submit_timestamps_in_rate_limits(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    state.record_submit_event(
        engine="counterbid",
        action_type="LIVE_COUNTERBID",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.4,
        status="submitted",
        reason="offer_id=bad-ts",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE execution_submit_log SET created_at = 'bad-ts' WHERE id = 1")

    with caplog.at_level("WARNING"):
        snapshot = state.get_rate_limit_snapshot(
            now=datetime.now(timezone.utc),
            max_live_offers_per_hour=10,
            max_bnb_per_day=5.0,
            submit_cooldown_seconds=30,
        )

    assert snapshot["hourly_count"] == 0
    assert snapshot["daily_bnb"] == 0.0
    assert snapshot["cooldown_remaining_seconds"] == 0
    assert snapshot["last_submit_at"] is None
    assert "Invalid timestamp for execution_submit_log[1].created_at" in caplog.text


def test_position_state_audit_integrity_quarantines_runtime_and_offer_state(tmp_path: Path) -> None:
    db_path = tmp_path / "execution.sqlite3"
    state = PositionState(db_path)
    state.upsert_active_offer(order_hash="broken-status", collection="0xabc", chain="bsc", price_bnb=0.5, status="mystery")
    state.set_runtime_value("live_armed_until", "bad-ts")
    state.set_runtime_value("live_armed_by", "operator")
    state.set_runtime_value("last_reconcile_chain", "eth")
    state.set_runtime_value("last_reconcile_local_added", "-1")
    state.record_submit_event(
        engine="counterbid",
        action_type="LIVE_COUNTERBID",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.4,
        status="submitted",
        reason="bad-submit-ts",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE execution_submit_log SET created_at = 'bad-ts' WHERE id = 1")

    audit = state.audit_integrity()

    assert audit.ok is False
    assert audit.issue_count >= 4
    assert "broken-status" in audit.offer_rows_quarantined
    assert "live_armed_until" in audit.runtime_keys_cleared
    assert "live_armed_by" in audit.runtime_keys_cleared
    assert "last_reconcile_chain" in audit.runtime_keys_cleared
    assert "last_reconcile_local_added" in audit.runtime_keys_cleared
    assert audit.malformed_submit_rows == [1]

    runtime = state.get_runtime_state()
    assert "live_armed_until" not in runtime
    assert "live_armed_by" not in runtime
    assert "last_reconcile_chain" not in runtime
    assert "last_reconcile_local_added" not in runtime
    assert runtime["last_integrity_issue_count"] == str(audit.issue_count)
    assert runtime["last_integrity_quarantine_count"] == str(audit.quarantine_count)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM active_offers WHERE order_hash = 'broken-status'").fetchone()
    assert row is not None
    assert row[0] == "state_invalid"


def test_position_state_audit_integrity_allows_retired_rows(tmp_path: Path) -> None:
    state = PositionState(tmp_path / "execution.sqlite3")
    state.upsert_active_offer(
        order_hash="retired-offer",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.5,
        status="retired",
    )

    audit = state.audit_integrity()

    assert audit.ok is True
    assert audit.offer_rows_quarantined == []
