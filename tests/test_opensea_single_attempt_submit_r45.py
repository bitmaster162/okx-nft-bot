from __future__ import annotations

from contextlib import contextmanager

import pytest

from okx_nft_bot.clients.http import HTTPStatusError, StdlibHttpTransport
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.sniper.counter_bidder import CounterBidder
from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT
from okx_nft_bot.sniper.opensea_receipt_reconciliation import (
    derive_seaport_order_hash,
    install_opensea_receipt_reconciliation,
)
from okx_nft_bot.sniper.opensea_single_attempt_submit_safety import (
    _SingleAttemptOpenSeaTransport,
    install_opensea_single_attempt_submit_safety,
)


OFFERER = "0x" + "1" * 40
COLLECTION = "0x" + "2" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ZONE = "0x000056f7000000ece9003ca63978907a00ffd100"
CONDUIT = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
ZERO32 = "0x" + "00" * 32
TARGET_URL = "https://api.opensea.test/api/v2/orders/ethereum/seaport/offers"
RPC_URL = "https://rpc.test"


def _parameters() -> dict:
    return {
        "offerer": OFFERER,
        "zone": ZONE,
        "offer": [
            {
                "itemType": 1,
                "token": WETH,
                "identifierOrCriteria": 0,
                "startAmount": "500",
                "endAmount": "500",
            }
        ],
        "consideration": [
            {
                "itemType": 4,
                "token": COLLECTION,
                "identifierOrCriteria": 0,
                "startAmount": "1",
                "endAmount": "1",
                "recipient": OFFERER,
            }
        ],
        "orderType": 2,
        "startTime": "1700000000",
        "endTime": "1700600000",
        "zoneHash": ZERO32,
        "salt": "123",
        "conduitKey": CONDUIT,
        "counter": "7",
        "totalOriginalConsiderationItems": 1,
    }


@contextmanager
def _mirror_scope():
    token = _MIRROR_CONTEXT.set({"bidder": object(), "halted": False})
    try:
        yield
    finally:
        _MIRROR_CONTEXT.reset(token)


class _NoopLimiter:
    def wait(self):
        return None


class _Response:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {}
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected extra HTTP attempt")
        return self.responses.pop(0)


def _transport(responses, *, max_retries=3):
    transport = StdlibHttpTransport(
        timeout=1,
        max_retries=max_retries,
        rate_limit_per_sec=1000,
    )
    transport._session = _Session(responses)
    transport._rate_limiter = _NoopLimiter()
    return transport


def test_r45_target_opensea_post_uses_exactly_one_stdlib_attempt(monkeypatch):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    transport = _transport([
        _Response(503, text="upstream unavailable"),
        _Response(200, {"order_hash": "0xshould-not-be-used"}),
    ])
    guarded = _SingleAttemptOpenSeaTransport(transport)

    with pytest.raises(HTTPStatusError) as exc_info:
        guarded.request_json(method="POST", url=TARGET_URL, headers={}, body="{}")

    assert exc_info.value.status == 503
    assert len(transport._session.calls) == 1
    assert transport.max_retries == 3


def test_r45_non_target_json_rpc_post_keeps_normal_retries(monkeypatch):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    transport = _transport([
        _Response(503, text="temporary rpc failure"),
        _Response(200, {"result": "0x01"}),
    ])
    guarded = _SingleAttemptOpenSeaTransport(transport)

    result = guarded.request_json(
        method="POST",
        url=RPC_URL,
        headers={"Content-Type": "application/json"},
        body='{"jsonrpc":"2.0"}',
    )

    assert result == {"result": "0x01"}
    assert len(transport._session.calls) == 2


def test_r45_unrelated_post_is_not_reclassified_as_effectful(monkeypatch):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    transport = _transport([
        _Response(503, text="temporary"),
        _Response(200, {"ok": True}),
    ])
    guarded = _SingleAttemptOpenSeaTransport(transport)

    result = guarded.request_json(
        method="POST",
        url="https://api.opensea.test/api/v2/other",
        headers={},
        body="{}",
    )

    assert result == {"ok": True}
    assert len(transport._session.calls) == 2


def test_r45_custom_transport_is_invoked_once_without_introspection():
    class CustomTransport:
        def __init__(self):
            self.calls = []

        def request_json(self, **kwargs):
            self.calls.append(kwargs)
            return {"order_hash": "0xcustom"}

    custom = CustomTransport()
    guarded = _SingleAttemptOpenSeaTransport(custom)

    result = guarded.request_json(method="POST", url=TARGET_URL, headers={}, body="{}")

    assert result == {"order_hash": "0xcustom"}
    assert len(custom.calls) == 1


def test_r45_direct_non_mirror_submit_keeps_original_contract():
    class DummyClient:
        def __init__(self):
            self.transport = object()
            self.calls = []

        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            self.calls.append((parameters, signature, chain, self.transport))
            return {"order_id": "direct"}

    install_opensea_single_attempt_submit_safety(DummyClient)
    client = DummyClient()
    original_transport = client.transport
    params = {"offerer": OFFERER}

    result = client._submit_opensea_offer(params, "0xsig", "eth")

    assert result == {"order_id": "direct"}
    assert client.calls == [(params, "0xsig", "eth", original_transport)]


def test_r45_r43_integration_single_post_then_exact_reconciliation(monkeypatch):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    params = _parameters()
    expected = derive_seaport_order_hash(params)
    transport = _transport([
        _Response(503, text="lost submit receipt"),
        _Response(200, {"order_hash": expected, "order_status": "active"}),
    ])

    class Settings:
        opensea_api_base = "https://api.opensea.test"
        opensea_api_key = "test-key"

    class DummyClient:
        def __init__(self):
            self.settings = Settings()
            self.transport = transport

        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            return self.transport.request_json(
                method="POST",
                url=TARGET_URL,
                headers={},
                body="{}",
            )

        def create_opensea_offer(self, *args, **kwargs):
            return {"order_id": "unused", "status": "submitted"}

    class DummyBidder:
        def _record_execution_submit_event(self, **kwargs):
            return None

    install_opensea_single_attempt_submit_safety(DummyClient)
    install_opensea_receipt_reconciliation(DummyBidder, DummyClient)
    client = DummyClient()

    with _mirror_scope():
        result = client._submit_opensea_offer(params, "0xsig", "eth")

    assert result["status"] == "submitted"
    assert result["reconciled"] is True
    assert result["order_id"] == expected
    assert [call["method"] for call in transport._session.calls] == ["POST", "GET"]


def test_r45_installer_is_idempotent():
    class DummyClient:
        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            return {"order_id": "x"}

    install_opensea_single_attempt_submit_safety(DummyClient)
    first = DummyClient._submit_opensea_offer
    install_opensea_single_attempt_submit_safety(DummyClient)
    assert DummyClient._submit_opensea_offer is first


def test_real_submit_wrapper_order_is_r43_r45_r44_r42_r41():
    current = OpenSeaClient._submit_opensea_offer
    assert getattr(current, "_r43_opensea_receipt_reconciliation", False)

    current = current.__wrapped__
    assert getattr(current, "_r45_opensea_single_attempt_submit", False)

    current = current.__wrapped__
    assert getattr(current, "_r44_opensea_conduit_allowance_guard", False)

    current = current.__wrapped__
    assert getattr(current, "_r42_opensea_effect_boundary_guard", False)

    current = current.__wrapped__
    assert getattr(current, "_r41_opensea_canonical_submit_route", False)


def test_r45_does_not_change_counterbid_record_wrapper():
    record = CounterBidder._record_execution_submit_event
    assert getattr(record, "_r43_opensea_uncertain_quarantine", False)
