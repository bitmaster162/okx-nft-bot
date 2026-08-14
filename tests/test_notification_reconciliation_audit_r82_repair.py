from __future__ import annotations

import pytest

from okx_nft_bot import cli_entry
from okx_nft_bot.storage.sqlite import SQLiteStore


CHANNEL = "telegram"
EVENT_ID = "evt-r82"
PAYLOAD = {"kind": "r82-test", "value": 1}
ACTOR = "ops-r82"
REASON = "operator independently verified notification outcome"


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "r82.sqlite3")


def test_mark_ambiguous_records_durable_operator_audit(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True

    assert store.mark_notification_attempt_ambiguous(
        CHANNEL,
        EVENT_ID,
        actor=ACTOR,
        reason="sender process confirmed stopped",
    ) is True

    rows = store.fetch_notification_resolutions(channel=CHANNEL, event_id=EVENT_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "active"
    assert rows[0]["resolution"] == "mark-ambiguous"
    assert rows[0]["actor"] == ACTOR
    assert rows[0]["reason"] == "sender process confirmed stopped"
    assert rows[0]["prior_payload_json"] is not None


def test_release_for_retry_keeps_history_after_attempt_is_deleted(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True
    assert store.mark_notification_attempt_ambiguous(
        CHANNEL,
        EVENT_ID,
        actor=ACTOR,
        reason="sender stopped",
    ) is True

    assert store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason="delivery confirmed absent",
    ) is True

    assert store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID) == []
    rows = store.fetch_notification_resolutions(channel=CHANNEL, event_id=EVENT_ID)
    assert [row["resolution"] for row in rows] == ["mark-ambiguous", "release-for-retry"]
    assert rows[-1]["prior_state"] == "ambiguous"
    assert rows[-1]["actor"] == ACTOR
    assert rows[-1]["reason"] == "delivery confirmed absent"


def test_mark_sent_records_audit_and_sent_receipt_atomically(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True

    assert store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="mark-sent",
        actor=ACTOR,
        reason="recipient-side evidence confirms delivery",
    ) is True

    assert store.was_notified(CHANNEL, EVENT_ID) is True
    assert store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID) == []
    rows = store.fetch_notification_resolutions(channel=CHANNEL, event_id=EVENT_ID)
    assert len(rows) == 1
    assert rows[0]["prior_state"] == "active"
    assert rows[0]["resolution"] == "mark-sent"
    assert rows[0]["actor"] == ACTOR


def test_failed_resolution_does_not_create_false_audit_event(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True

    assert store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="release-for-retry",
        actor=ACTOR,
        reason=REASON,
    ) is False
    assert store.fetch_notification_resolutions(channel=CHANNEL, event_id=EVENT_ID) == []


def test_cli_requires_actor_and_reason_for_notification_mutations() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([
            "mark-notification-attempt-ambiguous",
            "--channel",
            CHANNEL,
            "--event-id",
            EVENT_ID,
            "--sender-stopped",
            "--yes",
        ])

    marked = parser.parse_args([
        "mark-notification-attempt-ambiguous",
        "--channel",
        CHANNEL,
        "--event-id",
        EVENT_ID,
        "--sender-stopped",
        "--actor",
        ACTOR,
        "--reason",
        "sender process confirmed stopped",
        "--yes",
    ])
    assert marked.actor == ACTOR
    assert marked.reason == "sender process confirmed stopped"

    with pytest.raises(SystemExit):
        parser.parse_args([
            "resolve-notification-attempt",
            "--channel",
            CHANNEL,
            "--event-id",
            EVENT_ID,
            "--resolution",
            "mark-sent",
            "--yes",
        ])

    resolved = parser.parse_args([
        "resolve-notification-attempt",
        "--channel",
        CHANNEL,
        "--event-id",
        EVENT_ID,
        "--resolution",
        "mark-sent",
        "--actor",
        ACTOR,
        "--reason",
        REASON,
        "--yes",
    ])
    assert resolved.actor == ACTOR
    assert resolved.reason == REASON


def test_cli_exposes_read_only_notification_resolution_history() -> None:
    parser = cli_entry.build_parser()
    args = parser.parse_args([
        "notification-resolutions",
        "--channel",
        CHANNEL,
        "--event-id",
        EVENT_ID,
    ])
    assert args.command == "notification-resolutions"
