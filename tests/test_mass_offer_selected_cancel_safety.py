from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.mass_offer.engine import MassOfferEngine


class _Governor:
    def __init__(self, *, dry_run: bool, armed_states: list[bool]) -> None:
        self.dry_run = dry_run
        self.armed_states = list(armed_states)
        self.calls = 0

    def effective_dry_run(self, _configured=False):
        return self.dry_run

    def get_live_arm_state(self, *, now=None):
        index = min(self.calls, len(self.armed_states) - 1)
        armed = self.armed_states[index]
        self.calls += 1
        return {"armed": armed, "expires_at": None}


class _API:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str, object]] = []

    def cancel_offer(self, order_hash: str, *, chain: str, order_params=None):
        self.cancel_calls.append((order_hash, chain, order_params))
        return True


class _Tracker:
    def __init__(self) -> None:
        self.item_marks: list[tuple[int, str, str]] = []
        self.campaign_marks: list[tuple[int, str]] = []

    def mark_item_status(self, *, record_id: int, status: str, reason: str):
        self.item_marks.append((record_id, status, reason))

    def mark_campaign_status(self, *, campaign_id: int, status: str):
        self.campaign_marks.append((campaign_id, status))


class _State:
    def __init__(self) -> None:
        self.marks: list[tuple[str, str]] = []

    def mark_offer_status(self, *, order_hash: str, status: str):
        self.marks.append((order_hash, status))
        return True


def _record(record_id: int, campaign_id: int, token_id: int, offer_ref: str):
    return SimpleNamespace(
        record_id=record_id,
        campaign_id=campaign_id,
        token_id=token_id,
        offer_ref=offer_ref,
    )


def _engine(*, dry_run: bool, armed_states: list[bool], records=None):
    engine = MassOfferEngine.__new__(MassOfferEngine)
    engine.settings = SimpleNamespace(execution_chain="bsc")
    engine.governor = _Governor(dry_run=dry_run, armed_states=armed_states)
    engine.api_client = _API()
    engine.tracker = _Tracker()
    engine.state = _State()
    rows = list(records or [])
    engine._list_synced_active_records = lambda *, chain, collection=None: list(rows)
    engine._fetch_order_params = lambda order_hash, chain: {"order_hash": order_hash, "chain": chain}
    return engine


def test_force_dry_blocks_selected_live_cancel_without_state_mutation() -> None:
    engine = _engine(
        dry_run=True,
        armed_states=[True],
        records=[_record(1, 10, 101, "offer-a")],
    )

    payload = engine.cancel_selected(chain="bsc", order_hashes=["offer-a"])

    assert payload["selected_seen"] == 1
    assert payload["cancelled"] == 0
    assert payload["blocked_reason"] == "dry_run_enabled"
    assert payload["failed"] == ["offer-a:blocked:dry_run_enabled"]
    assert engine.api_client.cancel_calls == []
    assert engine.tracker.item_marks == []
    assert engine.state.marks == []


def test_disarmed_selected_live_cancel_is_blocked() -> None:
    engine = _engine(
        dry_run=False,
        armed_states=[False],
        records=[_record(1, 10, 101, "offer-a")],
    )

    payload = engine.cancel_selected(chain="bsc", order_hashes=["offer-a"])

    assert payload["cancelled"] == 0
    assert payload["blocked_reason"] == "live arm required"
    assert engine.api_client.cancel_calls == []
    assert engine.state.marks == []


def test_armed_selected_cancel_only_cancels_requested_active_hash() -> None:
    engine = _engine(
        dry_run=False,
        armed_states=[True, True],
        records=[
            _record(1, 10, 101, "offer-a"),
            _record(2, 20, 202, "offer-b"),
        ],
    )

    payload = engine.cancel_selected(chain="bsc", order_hashes=["offer-b"])

    assert payload == {
        "chain": "bsc",
        "selected_seen": 1,
        "cancelled": 1,
        "failed": [],
        "blocked_reason": None,
    }
    assert engine.api_client.cancel_calls == [
        ("offer-b", "bsc", {"order_hash": "offer-b", "chain": "bsc"})
    ]
    assert engine.tracker.item_marks == [
        (2, "cancelled", "mass_offer_unwind_cancel")
    ]
    assert engine.tracker.campaign_marks == [(20, "cancelled")]
    assert engine.state.marks == [("offer-b", "cancelled")]


def test_live_arm_is_rechecked_immediately_before_external_cancel() -> None:
    engine = _engine(
        dry_run=False,
        armed_states=[True, False],
        records=[_record(1, 10, 101, "offer-a")],
    )

    payload = engine.cancel_selected(chain="bsc", order_hashes=["offer-a"])

    assert payload["selected_seen"] == 1
    assert payload["cancelled"] == 0
    assert payload["failed"] == ["offer-a:blocked:live arm required"]
    assert engine.api_client.cancel_calls == []
    assert engine.tracker.item_marks == []
    assert engine.state.marks == []


def test_missing_selected_hash_is_reported_and_never_cancelled() -> None:
    engine = _engine(
        dry_run=False,
        armed_states=[True],
        records=[_record(1, 10, 101, "offer-a")],
    )

    payload = engine.cancel_selected(chain="bsc", order_hashes=["offer-missing"])

    assert payload["selected_seen"] == 0
    assert payload["cancelled"] == 0
    assert payload["failed"] == ["offer-missing:not_active"]
    assert engine.api_client.cancel_calls == []
