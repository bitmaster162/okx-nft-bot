from __future__ import annotations

from okx_nft_bot.mass_offer.engine import MassOfferEngine
from okx_nft_bot.mass_offer.scanner import MassOfferCandidate


class _GovernorStub:
    def check_live_submit_allowed(self, **kwargs):
        return None


class _APIStub:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = []

    def create_offer(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.response)


class _TrackerStub:
    def __init__(self) -> None:
        self.records = []

    def record_item(self, **kwargs):
        self.records.append(kwargs)


class _StateStub:
    def __init__(self) -> None:
        self.active_writes = []
        self.submit_events = []

    def upsert_active_offer(self, **kwargs):
        self.active_writes.append(kwargs)

    def record_submit_event(self, **kwargs):
        self.submit_events.append(kwargs)


def _engine(response: dict[str, object]):
    engine = object.__new__(MassOfferEngine)
    engine.governor = _GovernorStub()
    engine.api_client = _APIStub(response)
    engine.tracker = _TrackerStub()
    engine.state = _StateStub()
    return engine


def _target() -> MassOfferCandidate:
    return MassOfferCandidate(
        token_id=42,
        owner="0xowner",
        rarity="RARE",
        listed=False,
        existing_offer_bnb=None,
        raw={},
    )


def _submit(engine: MassOfferEngine):
    return engine._submit_target(
        campaign_id=7,
        collection="0xcollection",
        chain="bsc",
        target=_target(),
        offerer="0xbuyer",
        private_key="unused",
        price_bnb=0.25,
        price_wei=250000000000000000,
        counter=3,
        duration_seconds=3600,
        dry_run=False,
    )


def test_missing_offer_id_fails_without_local_active_write() -> None:
    engine = _engine({"offer_id": None, "status": "submitted"})

    result = _submit(engine)

    assert result.status == "failed"
    assert result.offer_ref is None
    assert result.reason == "no_offer_id_in_response"
    assert engine.state.active_writes == []

    assert len(engine.tracker.records) == 1
    assert engine.tracker.records[0]["status"] == "failed"
    assert engine.tracker.records[0]["reason"] == "no_offer_id_in_response"
    assert engine.tracker.records[0].get("offer_ref") is None

    assert len(engine.state.submit_events) == 1
    assert engine.state.submit_events[0]["status"] == "failed"
    assert engine.state.submit_events[0]["reason"] == "no_offer_id_in_response"


def test_real_offer_id_is_the_only_live_active_reference() -> None:
    engine = _engine({"offer_id": "order-abc", "status": "submitted"})

    result = _submit(engine)

    assert result.status == "active"
    assert result.offer_ref == "order-abc"
    assert result.reason == "submitted"

    assert len(engine.tracker.records) == 1
    assert engine.tracker.records[0]["status"] == "active"
    assert engine.tracker.records[0]["offer_ref"] == "order-abc"

    assert len(engine.state.active_writes) == 1
    assert engine.state.active_writes[0]["order_hash"] == "order-abc"
    assert engine.state.active_writes[0]["status"] == "active"

    assert len(engine.state.submit_events) == 1
    assert engine.state.submit_events[0]["status"] == "submitted"
    assert "offer_id=order-abc" in engine.state.submit_events[0]["reason"]
