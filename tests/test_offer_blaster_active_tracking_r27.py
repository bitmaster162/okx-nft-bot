from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.killswitch import _cancel_chain
from okx_nft_bot.sniper.offer_blaster import OfferBlaster
from okx_nft_bot.sniper.offer_blaster_accounting import install_offer_blaster_accounting
from okx_nft_bot.undercutter.state import PositionState


WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
COLLECTION = "0x" + "3" * 40


def _payload():
    wallet = "0x" + "1" * 40
    return {
        "chain": "eth",
        "offerer": wallet,
        "collection": COLLECTION,
        "token_id": 7,
        "parameters": {
            "offerer": wallet,
            "counter": "7",
            "offer": [
                {
                    "itemType": 1,
                    "token": WETH_ADDRESS,
                    "identifierOrCriteria": 0,
                    "startAmount": str(10**18),
                    "endAmount": str(10**18),
                }
            ],
            "consideration": [
                {
                    "itemType": 2,
                    "token": COLLECTION,
                    "identifierOrCriteria": 7,
                    "startAmount": "1",
                    "endAmount": "1",
                    "recipient": wallet,
                }
            ],
        },
        "signature": "0xsig",
    }


def _fake_classes(db_path: Path, *, repeat=1, catch_errors=False):
    class FakeClient:
        submit_calls = 0

        def __init__(self):
            self.settings = SimpleNamespace(
                execution_db_path=db_path,
                dry_run=False,
            )

        def submit_offer(self, payload):
            self.__class__.submit_calls += 1
            _ = payload
            return {"offer_id": "offer-eth-1"}

    class FakeBlaster:
        def __init__(self):
            self.execution_db_path = db_path

        def _blast_eth(self, client, payload):
            results = []
            for _ in range(repeat):
                if catch_errors:
                    try:
                        results.append(client.submit_offer(payload))
                    except Exception as exc:
                        results.append(exc)
                else:
                    results.append(client.submit_offer(payload))
            return results

    install_offer_blaster_accounting(FakeBlaster, FakeClient)
    return FakeBlaster, FakeClient


def test_real_classes_have_r27_active_offer_tracking():
    assert getattr(OfferBlaster._blast_eth, "_r27_active_offer_tracking", False) is True
    assert getattr(OKXAPIClient.submit_offer, "_r27_active_offer_tracking", False) is True
    assert getattr(OfferBlaster._blast_eth, "_r26_accounting_context", False) is True
    assert getattr(OKXAPIClient.submit_offer, "_r26_accounting_guard", False) is True


def test_active_offer_is_persisted_before_submit_ledger(monkeypatch, tmp_path):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    events = []

    class FakeState:
        def __init__(self, db_path):
            assert db_path == tmp_path / "execution.sqlite3"

        def upsert_active_offer(self, **kwargs):
            events.append(("active", kwargs))

        def record_submit_event(self, **kwargs):
            events.append(("submit", kwargs))
            return 1

        def set_force_dry_run(self, *_args, **_kwargs):
            pytest.fail("force dry-run must not run on successful state tracking")

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    db_path = tmp_path / "execution.sqlite3"
    FakeBlaster, FakeClient = _fake_classes(db_path)

    result = FakeBlaster()._blast_eth(FakeClient(), _payload())

    assert result == [{"offer_id": "offer-eth-1"}]
    assert [name for name, _ in events] == ["active", "submit"]
    assert events[0][1] == {
        "order_hash": "offer-eth-1",
        "collection": COLLECTION,
        "chain": "eth",
        "price_bnb": 0.5,
        "status": "active",
    }
    assert events[1][1]["status"] == "submitted"
    assert events[1][1]["price_bnb"] == 0.5


def test_ledger_failure_keeps_active_row_then_forces_safe(monkeypatch, tmp_path):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    events = []
    forced = []

    class FakeState:
        def __init__(self, _db_path):
            pass

        def upsert_active_offer(self, **kwargs):
            events.append(("active", kwargs["order_hash"]))

        def record_submit_event(self, **_kwargs):
            events.append(("submit", "failed"))
            raise RuntimeError("ledger unavailable")

        def set_force_dry_run(self, enabled, *, reason=None):
            forced.append((enabled, reason))

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    FakeBlaster, FakeClient = _fake_classes(
        tmp_path / "execution.sqlite3",
        repeat=2,
        catch_errors=True,
    )
    blaster = FakeBlaster()
    client = FakeClient()

    results = blaster._blast_eth(client, _payload())

    assert FakeClient.submit_calls == 1
    assert events == [("active", "offer-eth-1"), ("submit", "failed")]
    assert "ledger unavailable" in str(results[0])
    assert "live submits halted after accounting failure" in str(results[1])
    assert client.settings.dry_run is True
    assert forced == [(True, "offer_blaster_submit_log_failure")]


def test_active_upsert_failure_blocks_ledger_and_remaining_submit(monkeypatch, tmp_path):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    ledger_calls = []
    forced = []

    class FakeState:
        def __init__(self, _db_path):
            pass

        def upsert_active_offer(self, **_kwargs):
            raise RuntimeError("active state unavailable")

        def record_submit_event(self, **kwargs):
            ledger_calls.append(kwargs)

        def set_force_dry_run(self, enabled, *, reason=None):
            forced.append((enabled, reason))

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    FakeBlaster, FakeClient = _fake_classes(
        tmp_path / "execution.sqlite3",
        repeat=2,
        catch_errors=True,
    )
    client = FakeClient()

    results = FakeBlaster()._blast_eth(client, _payload())

    assert FakeClient.submit_calls == 1
    assert ledger_calls == []
    assert "active state unavailable" in str(results[0])
    assert "live submits halted after accounting failure" in str(results[1])
    assert client.settings.dry_run is True
    assert forced == [(True, "offer_blaster_submit_log_failure")]


def test_degraded_killswitch_uses_r27_local_active_offer(monkeypatch, tmp_path):
    import okx_nft_bot.counterbid.submit_safety as submit_safety

    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    db_path = tmp_path / "execution.sqlite3"
    FakeBlaster, FakeClient = _fake_classes(db_path)
    client = FakeClient()

    FakeBlaster()._blast_eth(client, _payload())

    state = PositionState(db_path)
    active = state.get_active_offers(chain="eth")
    assert len(active) == 1
    assert active[0].order_hash == "offer-eth-1"
    assert active[0].collection == COLLECTION
    assert active[0].price_bnb == 0.5

    class DegradedAPI:
        def __init__(self):
            self.cancelled = []

        def get_my_offers(self, **_kwargs):
            raise RuntimeError("exchange lookup unavailable")

        def cancel_offer(self, order_hash, *, chain, order_params=None):
            self.cancelled.append((order_hash, chain, order_params))
            return True

    api = DegradedAPI()
    result = _cancel_chain(state=state, api=api, chain="eth")

    assert result.exchange_lookup_failed is True
    assert result.active_offers_seen == 1
    assert result.exchange_seen == 1
    assert result.live_cancelled == 1
    assert result.failure_count == 1
    assert api.cancelled == [("offer-eth-1", "eth", None)]
    assert state.get_active_offers(chain="eth") == []
