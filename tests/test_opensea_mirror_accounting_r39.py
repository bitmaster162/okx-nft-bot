from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.sniper.counter_bidder import CounterBidder
from okx_nft_bot.sniper.opensea_mirror_safety import install_opensea_mirror_safety


WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
COLLECTION = "0x" + "3" * 40


class _State:
    def __init__(self, *, fail_record: bool = False):
        self.fail_record = fail_record
        self.events = []
        self.forced = []

    def record_submit_event(self, **kwargs):
        if self.fail_record:
            raise RuntimeError("sqlite unavailable")
        self.events.append(kwargs)
        return len(self.events)

    def set_force_dry_run(self, enabled, *, reason=None):
        self.forced.append((enabled, reason))


class _Governor:
    def __init__(self, *, hourly_count=1, daily_bnb=0.5, hourly_limit=20, daily_limit=5.0):
        self.snapshot = {
            "hourly_count": hourly_count,
            "daily_bnb": daily_bnb,
        }
        self.settings = SimpleNamespace(
            max_live_offers_per_hour=hourly_limit,
            max_bnb_per_day=daily_limit,
        )

    def get_rate_limit_snapshot(self, *, chain):
        assert chain == "eth"
        return dict(self.snapshot)


def _fake_classes(*, governor=None, state=None):
    resolved_governor = governor or _Governor()
    resolved_state = state or _State()

    class FakeOpenSeaClient:
        calls = 0

        def create_opensea_offer(self, **kwargs):
            self.__class__.calls += 1
            return {"order_id": "os-order-1", "offer_id": "os-order-1", "status": "submitted"}

    class FakeBidder:
        def __init__(self):
            self.opensea_client = FakeOpenSeaClient()
            self.dry_run = False
            self.legacy_events = []

        def _get_execution_governor(self):
            return resolved_governor

        def _get_execution_state(self):
            return resolved_state

        def _record_execution_submit_event(
            self,
            *,
            chain,
            collection,
            price_bnb,
            status,
            reason,
        ):
            self.legacy_events.append(
                {
                    "chain": chain,
                    "collection": collection,
                    "price_bnb": price_bnb,
                    "status": status,
                    "reason": reason,
                }
            )

        def _mirror_to_opensea(self, collection_address, price, currency, duration_hours=720):
            try:
                result = self.opensea_client.create_opensea_offer(
                    chain="eth",
                    collection_address=collection_address,
                    token_id=None,
                    price_wei=int(round(float(price) * 10**18)),
                    currency_address=WETH_ADDRESS,
                    valid_time=123456,
                )
            except Exception as exc:
                self._record_execution_submit_event(
                    chain="eth",
                    collection=collection_address,
                    price_bnb=price,
                    status="failed",
                    reason=f"opensea: {exc}",
                )
                return False

            order_id = result["order_id"]
            self._record_execution_submit_event(
                chain="eth",
                collection=collection_address,
                price_bnb=price,
                status="submitted",
                reason=f"opensea order_id={order_id}",
            )
            return True

    install_opensea_mirror_safety(FakeBidder, FakeOpenSeaClient)
    return FakeBidder, FakeOpenSeaClient, resolved_state


def test_real_classes_have_r39_mirror_safety_installed():
    assert getattr(CounterBidder._mirror_to_opensea, "_r39_opensea_mirror_context", False) is True
    assert getattr(OpenSeaClient.create_opensea_offer, "_r39_opensea_mirror_gate", False) is True
    assert getattr(CounterBidder._record_execution_submit_event, "_r39_opensea_mirror_accounting", False) is True


def test_durable_mirror_records_bnb_equivalent_once(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    observed = []

    def normalize(**kwargs):
        observed.append(kwargs)
        return 0.25, 150.0

    monkeypatch.setattr(submit_safety, "_buy_price_bnb_equiv", normalize)
    FakeBidder, FakeClient, state = _fake_classes()
    bidder = FakeBidder()

    assert bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH") is True
    assert FakeClient.calls == 1
    assert observed == [
        {
            "chain_name": "eth",
            "requirements": {WETH_ADDRESS: 5 * 10**16},
        }
    ]
    assert bidder.legacy_events == []
    assert state.events == [
        {
            "engine": "counter_bidder",
            "action_type": "LIVE_OPENSEA_MIRROR",
            "collection": COLLECTION,
            "chain": "eth",
            "price_bnb": 0.25,
            "status": "submitted",
            "reason": "opensea order_id=os-order-1;price_usd=150.00000000",
        }
    ]


def test_incremental_daily_cap_blocks_before_opensea_effect(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.2, 120.0),
    )
    governor = _Governor(daily_bnb=4.9, daily_limit=5.0)
    FakeBidder, FakeClient, state = _fake_classes(governor=governor)
    bidder = FakeBidder()

    assert bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH") is False
    assert FakeClient.calls == 0
    assert state.events == []
    assert len(bidder.legacy_events) == 1
    assert bidder.legacy_events[0]["status"] == "failed"
    assert "daily BNB-equivalent cap hit" in bidder.legacy_events[0]["reason"]


def test_incremental_hourly_limit_blocks_before_opensea_effect(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.1, 60.0),
    )
    governor = _Governor(hourly_count=20, hourly_limit=20)
    FakeBidder, FakeClient, state = _fake_classes(governor=governor)
    bidder = FakeBidder()

    assert bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH") is False
    assert FakeClient.calls == 0
    assert state.events == []
    assert "rate limit hit" in bidder.legacy_events[0]["reason"]


def test_price_normalization_uncertainty_blocks_before_opensea_effect(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    def fail_normalize(**_kwargs):
        raise RuntimeError("BNB/USD price unavailable")

    monkeypatch.setattr(submit_safety, "_buy_price_bnb_equiv", fail_normalize)
    FakeBidder, FakeClient, state = _fake_classes()
    bidder = FakeBidder()

    assert bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH") is False
    assert FakeClient.calls == 0
    assert state.events == []
    assert "BNB/USD price unavailable" in bidder.legacy_events[0]["reason"]


def test_post_submit_accounting_failure_forces_safe_state(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.25, 150.0),
    )
    state = _State(fail_record=True)
    FakeBidder, FakeClient, _ = _fake_classes(state=state)
    bidder = FakeBidder()

    with pytest.raises(
        RuntimeError,
        match="OpenSea mirror post-submit accounting failed after durable effect",
    ):
        bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH")

    assert FakeClient.calls == 1
    assert bidder.dry_run is True
    assert state.forced == [(True, "opensea_mirror_submit_log_failure")]

    with pytest.raises(RuntimeError, match="OpenSea mirror halted after submit accounting failure"):
        bidder._mirror_to_opensea(COLLECTION, 0.05, "WETH")
    assert FakeClient.calls == 1


def test_non_mirror_opensea_call_is_untouched():
    FakeBidder, FakeClient, state = _fake_classes()
    _ = FakeBidder, state
    client = FakeClient()

    result = client.create_opensea_offer(
        chain="eth",
        collection_address=COLLECTION,
        token_id=None,
        price_wei=10**17,
        currency_address=WETH_ADDRESS,
        valid_time=123456,
    )

    assert result["order_id"] == "os-order-1"
    assert FakeClient.calls == 1


def test_installer_is_idempotent_for_same_classes():
    FakeBidder, FakeClient, _state = _fake_classes()
    mirror = FakeBidder._mirror_to_opensea
    create = FakeClient.create_opensea_offer
    record = FakeBidder._record_execution_submit_event

    install_opensea_mirror_safety(FakeBidder, FakeClient)

    assert FakeBidder._mirror_to_opensea is mirror
    assert FakeClient.create_opensea_offer is create
    assert FakeBidder._record_execution_submit_event is record
