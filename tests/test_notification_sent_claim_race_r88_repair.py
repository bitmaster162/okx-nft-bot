from __future__ import annotations

from okx_nft_bot.storage.sqlite import SQLiteStore


def test_sent_receipt_blocks_stale_notification_claim_acquire(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "r88.sqlite3")
    channel = "test"
    event_id = "evt-r88"
    payload = {"event_id": event_id}

    # Worker B observes the pre-send state and keeps this stale decision.
    assert store.was_notified(channel, event_id) is False

    # Worker A acquires the delivery claim, sends successfully, records the
    # durable receipt, and clears its claim before worker B reaches acquire.
    assert store.begin_notification_attempt(channel, event_id, payload=payload) is True
    store.mark_notified(channel, event_id, payload=payload)
    assert store.was_notified(channel, event_id) is True
    assert store.fetch_notification_attempts(channel=channel, event_id=event_id) == []

    # The acquire operation itself must re-check durable sent state atomically.
    # Otherwise worker B can create a fresh claim from its stale pre-send read
    # and send the same notification again.
    assert store.begin_notification_attempt(channel, event_id, payload=payload) is False
    assert store.fetch_notification_attempts(channel=channel, event_id=event_id) == []
