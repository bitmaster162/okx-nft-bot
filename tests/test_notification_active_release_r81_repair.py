from __future__ import annotations

import pytest

from okx_nft_bot import cli_entry
from okx_nft_bot.storage.sqlite import SQLiteStore


CHANNEL = "telegram"
EVENT_ID = "evt-r81"
PAYLOAD = {"kind": "r81-test", "value": 1}


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "r81.sqlite3")


def test_fresh_notification_attempt_is_active_and_cannot_be_released_for_retry(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True

    rows = store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "active"

    assert store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="release-for-retry",
    ) is False

    rows = store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "active"


def test_operator_must_mark_attempt_ambiguous_before_release_for_retry(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.begin_notification_attempt(CHANNEL, EVENT_ID, payload=PAYLOAD) is True

    assert store.mark_notification_attempt_ambiguous(CHANNEL, EVENT_ID) is True
    rows = store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10)
    assert len(rows) == 1
    assert rows[0]["state"] == "ambiguous"

    assert store.resolve_notification_attempt(
        CHANNEL,
        EVENT_ID,
        resolution="release-for-retry",
    ) is True
    assert store.fetch_notification_attempts(channel=CHANNEL, event_id=EVENT_ID, limit=10) == []
    assert store.was_notified(CHANNEL, EVENT_ID) is False


def test_cli_requires_sender_stopped_attestation_for_ambiguous_transition() -> None:
    parser = cli_entry.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([
            "mark-notification-attempt-ambiguous",
            "--channel",
            CHANNEL,
            "--event-id",
            EVENT_ID,
            "--yes",
        ])

    args = parser.parse_args([
        "mark-notification-attempt-ambiguous",
        "--channel",
        CHANNEL,
        "--event-id",
        EVENT_ID,
        "--sender-stopped",
        "--yes",
    ])
    assert args.command == "mark-notification-attempt-ambiguous"
    assert args.sender_stopped is True
    assert args.yes is True


def test_direct_ambiguous_transition_fails_closed_without_attestation() -> None:
    with pytest.raises(SystemExit, match="sender-stopped"):
        cli_entry.cmd_mark_notification_attempt_ambiguous(
            channel=CHANNEL,
            event_id=EVENT_ID,
            sender_stopped=False,
            force=True,
        )
