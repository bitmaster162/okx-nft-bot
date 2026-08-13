from __future__ import annotations

import pytest

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.counterbid.cancel_effect_safety import install_cancel_effect_safety


CANCEL_PATH = "/api/v5/mktplace/nft/markets/cancel-listing"


class _Settings:
    okx_api_base = "https://okx.test"
    buyer_wallet_address = "0x00000000000000000000000000000000000000aa"


class _NoWaitLimiter:
    def wait(self):
        return None


class _Response:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": "0"}
        self.text = "simulated"
        self.headers = {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        if not self.responses:
            raise AssertionError("unexpected extra HTTP attempt")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _MarketClient:
    def __init__(self, transport):
        self.transport = transport
        self.header_calls = 0

    def _build_headers(self, *, method, request_path, body):
        self.header_calls += 1
        return {"X-Test": f"{method}:{request_path}:{len(body)}"}


class _DummyClient:
    def __init__(self, transport, *, readback=None, readback_exc=None):
        self.settings = _Settings()
        self.market = _MarketClient(transport)
        self.original_calls = []
        self.onchain_calls = []
        self.readback_calls = []
        self.readback = list(readback or [])
        self.readback_exc = readback_exc

    def _market_client(self):
        return self.market

    def _request(self, *, method, path, params=None, payload=None):
        self.original_calls.append((method, path, params, payload))
        return self.market.transport.request_json(
            method=method,
            url=f"https://okx.test{path}",
            headers={},
            body="",
        )

    def cancel_offer(self, offer_id: str, chain: str = "bsc", order_params=None):
        raise AssertionError("legacy cancel_offer must be replaced by R57")

    def _cancel_onchain_seaport(self, order_params, chain):
        self.onchain_calls.append((order_params, chain))
        return True

    def get_my_offers(self, chain="bsc", collection_address="", *, require_all_endpoints=False):
        self.readback_calls.append((chain, collection_address, require_all_endpoints))
        if self.readback_exc is not None:
            raise self.readback_exc
        return list(self.readback)


def _guarded(transport, *, readback=None, readback_exc=None):
    class Client(_DummyClient):
        pass

    install_cancel_effect_safety(Client)
    return Client(transport, readback=readback, readback_exc=readback_exc)


def _stdlib_transport(*responses):
    transport = StdlibHttpTransport(
        timeout=1,
        max_retries=4,
        rate_limit_per_sec=1000.0,
    )
    transport._rate_limiter = _NoWaitLimiter()
    transport._session = _Session(responses)
    return transport


@pytest.mark.parametrize("status", [429, 503])
def test_ambiguous_http_cancel_uses_one_attempt_and_never_falls_back_onchain(status):
    transport = _stdlib_transport(
        _Response(status),
        _Response(200, {"code": "0"}),
    )
    client = _guarded(
        transport,
        readback=[{"orderId": "offer-r57"}],
    )

    result = client.cancel_offer(
        "offer-r57",
        chain="bsc",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert transport._session.calls == 1
    assert transport.max_retries == 4
    assert client.onchain_calls == []
    assert client.readback_calls == [("bsc", "", True)]
    assert client.original_calls == []


def test_ambiguous_transport_failure_is_read_back_and_never_falls_back_onchain():
    class FailingTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            raise RuntimeError("connection reset after cancel send")

    transport = FailingTransport()
    client = _guarded(transport, readback=[])

    result = client.cancel_offer(
        "offer-r57",
        chain="eth",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert transport.calls == 1
    assert client.onchain_calls == []
    assert client.readback_calls == [("eth", "", True)]


def test_strict_readback_failure_after_ambiguous_cancel_retains_exposure():
    transport = _stdlib_transport(_Response(503))
    client = _guarded(
        transport,
        readback_exc=RuntimeError("inventory unavailable"),
    )

    result = client.cancel_offer(
        "offer-r57",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert client.onchain_calls == []
    assert client.readback_calls == [("bsc", "", True)]


def test_absent_page_limited_readback_is_not_promoted_to_cancel_success():
    transport = _stdlib_transport(_Response(503))
    client = _guarded(
        transport,
        readback=[{"orderId": "some-other-offer"}],
    )

    result = client.cancel_offer(
        "offer-r57",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert client.onchain_calls == []
    assert client.readback_calls == [("bsc", "", True)]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_deterministic_http_rejection_may_use_existing_onchain_fallback(status):
    transport = _stdlib_transport(_Response(status))
    client = _guarded(transport)
    params = {"offerer": "0x1", "counter": 7}

    result = client.cancel_offer(
        "offer-r57",
        chain="bsc",
        order_params=params,
    )

    assert result is True
    assert transport._session.calls == 1
    assert client.onchain_calls == [(params, "bsc")]
    assert client.readback_calls == []


def test_deterministic_application_rejection_preserves_onchain_fallback():
    class ResultTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            return {"code": "1", "msg": "No longer available"}

    transport = ResultTransport()
    client = _guarded(transport)
    params = {"offerer": "0x1", "counter": 8}

    result = client.cancel_offer(
        "offer-r57",
        chain="eth",
        order_params=params,
    )

    assert result is True
    assert transport.calls == 1
    assert client.onchain_calls == [(params, "eth")]
    assert client.readback_calls == []


def test_successful_api_cancel_returns_true_without_readback_or_onchain():
    class ResultTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            return {"code": "0", "data": {"success": True}}

    transport = ResultTransport()
    client = _guarded(transport)

    result = client.cancel_offer(
        "offer-r57",
        order_params={"offerer": "0x1"},
    )

    assert result is True
    assert transport.calls == 1
    assert client.onchain_calls == []
    assert client.readback_calls == []


def test_malformed_cancel_receipt_is_fail_closed_and_suppresses_onchain():
    class MalformedTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            return ["unexpected"]

    transport = MalformedTransport()
    client = _guarded(transport, readback=[])

    result = client.cancel_offer(
        "offer-r57",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert transport.calls == 1
    assert client.onchain_calls == []
    assert client.readback_calls == [("bsc", "", True)]


def test_non_cancel_request_keeps_existing_transport_retry_policy(monkeypatch):
    transport = _stdlib_transport(
        _Response(429),
        _Response(200, {"code": "0", "data": []}),
    )
    monkeypatch.setattr(
        StdlibHttpTransport,
        "_sleep_backoff",
        lambda self, attempt, headers: None,
    )
    client = _guarded(transport)

    result = client._request(
        method="GET",
        path="/api/v5/mktplace/nft/markets/offers",
        params={"limit": 1},
    )

    assert result["code"] == "0"
    assert transport._session.calls == 2
    assert transport.max_retries == 4
    assert len(client.original_calls) == 1


def test_installer_is_idempotent():
    class Client(_DummyClient):
        pass

    install_cancel_effect_safety(Client)
    request_once = Client._request
    cancel_once = Client.cancel_offer
    install_cancel_effect_safety(Client)

    assert Client._request is request_once
    assert Client.cancel_offer is cancel_once
    assert getattr(Client._request, "_r57_cancel_single_attempt_guard", False) is True
    assert getattr(Client.cancel_offer, "_r57_cancel_ambiguity_guard", False) is True
