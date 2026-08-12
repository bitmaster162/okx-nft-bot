from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from okx_nft_bot.sniper.offer_blaster import OfferBlaster
from okx_nft_bot.sniper.offer_blaster_accounting import install_offer_blaster_accounting
from okx_nft_bot.counterbid.okx_api import OKXAPIClient


WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


class _ActiveStateStub:
    def upsert_active_offer(self, **_kwargs):
        return None


def _payload(*, chain="eth", offer_id="offer-1"):
    _ = offer_id
    wallet = "0x" + "1" * 40
    collection = "0x" + "3" * 40
    return {
        "chain": chain,
        "offerer": wallet,
        "collection": collection,
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
                    "token": collection,
                    "identifierOrCriteria": 7,
                    "startAmount": "1",
                    "endAmount": "1",
                    "recipient": wallet,
                }
            ],
        },
        "signature": "0xsig",
    }


def _fake_classes(*, submit_result=None, repeat=1, catch_errors=False):
    class FakeClient:
        submit_calls = 0

        def __init__(self):
            self.settings = SimpleNamespace(
                execution_db_path=Path("/tmp/r26-execution.sqlite3"),
                dry_run=False,
            )

        def submit_offer(self, payload):
            self.__class__.submit_calls += 1
            _ = payload
            return submit_result or {"offer_id": "offer-1"}

    class FakeBlaster:
        def __init__(self):
            self.execution_db_path = Path("/tmp/r26-execution.sqlite3")

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


def test_real_classes_have_r26_accounting_installed():
    assert getattr(OfferBlaster._blast_eth, "_r26_accounting_context", False) is True
    assert getattr(OKXAPIClient.submit_offer, "_r26_accounting_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r25_receipt_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r24_priced_governor_guard", False) is True


def test_non_blaster_submit_is_not_double_counted(monkeypatch):
    import okx_nft_bot.undercutter.state as state_module

    class ForbiddenState:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("non-Blaster submit must not touch OfferBlaster accounting")

    monkeypatch.setattr(state_module, "PositionState", ForbiddenState)
    FakeBlaster, FakeClient = _fake_classes()
    _ = FakeBlaster
    client = FakeClient()

    result = client.submit_offer(_payload())

    assert result == {"offer_id": "offer-1"}
    assert FakeClient.submit_calls == 1


def test_eth_blaster_durable_submit_records_bnb_equivalent_once(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    events = []

    class FakeState(_ActiveStateStub):
        def __init__(self, db_path):
            assert db_path == Path("/tmp/r26-execution.sqlite3")

        def record_submit_event(self, **kwargs):
            events.append(kwargs)
            return 1

        def set_force_dry_run(self, *_args, **_kwargs):
            pytest.fail("force dry-run must not run on successful accounting")

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    FakeBlaster, FakeClient = _fake_classes()
    blaster = FakeBlaster()
    client = FakeClient()

    result = blaster._blast_eth(client, _payload())

    assert result == [{"offer_id": "offer-1"}]
    assert FakeClient.submit_calls == 1
    assert events == [
        {
            "engine": "offer_blaster",
            "action_type": "LIVE_OFFER_BLAST",
            "collection": "0x" + "3" * 40,
            "chain": "eth",
            "price_bnb": 0.5,
            "status": "submitted",
            "reason": "offer_id=offer-1;token_id=7;price_usd=300.00000000",
        }
    ]


def test_accounting_uses_signed_erc20_requirement_for_r24_normalization(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    observed = []

    class FakeState(_ActiveStateStub):
        def __init__(self, _db_path):
            pass

        def record_submit_event(self, **_kwargs):
            return 1

    def normalize(**kwargs):
        observed.append(kwargs)
        return 0.25, 150.0

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(submit_safety, "_buy_price_bnb_equiv", normalize)
    FakeBlaster, FakeClient = _fake_classes()

    FakeBlaster()._blast_eth(FakeClient(), _payload())

    assert observed == [
        {
            "chain_name": "eth",
            "requirements": {WETH_ADDRESS: 10**18},
        }
    ]


def test_accounting_failure_forces_safe_state_and_halts_remaining_blast(monkeypatch):
    import okx_nft_bot.counterbid.submit_safety as submit_safety
    import okx_nft_bot.undercutter.state as state_module

    forced = []

    class FakeState(_ActiveStateStub):
        def __init__(self, _db_path):
            pass

        def record_submit_event(self, **_kwargs):
            raise RuntimeError("sqlite unavailable")

        def set_force_dry_run(self, enabled, *, reason=None):
            forced.append((enabled, reason))

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    monkeypatch.setattr(
        submit_safety,
        "_buy_price_bnb_equiv",
        lambda **_kwargs: (0.5, 300.0),
    )
    FakeBlaster, FakeClient = _fake_classes(repeat=2, catch_errors=True)
    blaster = FakeBlaster()
    client = FakeClient()

    results = blaster._blast_eth(client, _payload())

    assert FakeClient.submit_calls == 1
    assert len(results) == 2
    assert "post-submit accounting failed after durable effect" in str(results[0])
    assert "sqlite unavailable" in str(results[0])
    assert "live submits halted after accounting failure" in str(results[1])
    assert client.settings.dry_run is True
    assert forced == [(True, "offer_blaster_submit_log_failure")]


def test_missing_durable_receipt_fails_safe_after_submit(monkeypatch):
    import okx_nft_bot.undercutter.state as state_module

    forced = []

    class FakeState(_ActiveStateStub):
        def __init__(self, _db_path):
            pass

        def record_submit_event(self, **_kwargs):
            pytest.fail("missing durable receipt must not be logged as submitted")

        def set_force_dry_run(self, enabled, *, reason=None):
            forced.append((enabled, reason))

    monkeypatch.setattr(state_module, "PositionState", FakeState)
    FakeBlaster, FakeClient = _fake_classes(
        submit_result={"offer_id": "pending"},
        catch_errors=True,
    )
    client = FakeClient()

    results = FakeBlaster()._blast_eth(client, _payload())

    assert FakeClient.submit_calls == 1
    assert "durable OKX offer receipt unavailable" in str(results[0])
    assert client.settings.dry_run is True
    assert forced == [(True, "offer_blaster_submit_log_failure")]


def test_unexpected_chain_blocks_before_okx_effect():
    FakeBlaster, FakeClient = _fake_classes(catch_errors=True)
    client = FakeClient()

    results = FakeBlaster()._blast_eth(client, _payload(chain="bsc"))

    assert FakeClient.submit_calls == 0
    assert "unexpected chain 'bsc'" in str(results[0])


def test_installer_is_idempotent_for_same_classes():
    FakeBlaster, FakeClient = _fake_classes()
    blast_method = FakeBlaster._blast_eth
    submit_method = FakeClient.submit_offer

    install_offer_blaster_accounting(FakeBlaster, FakeClient)

    assert FakeBlaster._blast_eth is blast_method
    assert FakeClient.submit_offer is submit_method
