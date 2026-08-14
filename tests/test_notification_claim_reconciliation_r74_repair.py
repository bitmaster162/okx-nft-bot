from __future__ import annotations

import json

from okx_nft_bot.cli_entry import build_parser
from okx_nft_bot.storage.sqlite import SQLiteStore


CHANNEL = "telegram"
EVENT_ID = "evt-r74"
PAYLOAD = {"kind": "r74-test", "value": 1}


def _seed_attempt(store: SQLiteStore) -> None:
    created = store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD)
    assert created is True


def test_ambiguous_notification_attempts_are_inspectable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "r74.sqlite3")
    _seed_attempt(store)

    rows = store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == CHANNEL
    assert row["event_id"] == EVENT_ID
    assert row["started_at"]
    assert json.loads(row["payload_json"]) == PAYLOAD


def test_operator_can_mark_ambiguous_attempt_as_sent_atomically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "r74.sqlite3")
    _seed_attempt(store)

    resolved = store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="mark-sent",
    )

    assert resolved is True
    assert store.was_notified(CHANNEL, EVENT_ID) is True
    assert store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10) == []


def test_operator_can_release_ambiguous_attempt_for_retry_without_marking_sent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "r74.sqlite3")
    _seed_attempt(store)

    resolved = store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="release-for-retry",
    )

    assert resolved is True
    assert store.was_notified(CHANNEL, EVENT_ID) is False
    assert store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10) == []
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True


def test_resolution_of_missing_attempt_is_a_noop(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "r74.sqlite3")

    resolved = store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="mark-sent",
    )

    assert resolved is False
    assert store.was_notified(CHANNEL, EVENT_ID) is False


def test_cli_exposes_notification_attempt_listing_command() -> None:
    args = build_parser().parse_args(
        [
            "notification-attempts",
            "--channel",
            CHANNEL,
            "--event-id",
            EVENT_ID,
            "--limit",
            "7",
        ]
    )

    assert args.command == "notification-attempts"
    assert args.channel == CHANNEL
    assert args.event_id == EVENT_ID
    assert args.limit == 7


def test_cli_exposes_explicit_notification_attempt_resolution_command() -> None:
    args = build_parser().parse_args(
        [
            "resolve-notification-attempt",
            "--channel",
            CHANNEL,
            "--event-id",
            EVENT_ID,
            "--resolution",
            "release-for-retry",
            "--yes",
        ]
    )

    assert args.command == "resolve-notification-attempt"
    assert args.channel == CHANNEL
    assert args.event_id == EVENT_ID
    assert args.resolution == "release-for-retry"
    assert args.yes is True
