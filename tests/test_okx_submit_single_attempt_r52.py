from __future__ import annotations

import inspect

import pytest

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.counterbid.okx_api import (
    OKXNetworkError,
    OKXRateLimitError,
    OKXSubmitError,
)
from okx_nft_bot.counterbid.receipt_safety import install_receipt_safety
from okx_nft_bot.counterbid.submit_safety import install_submit_safety
from okx_nft_bot.counterbid.submit_single_attempt_safety import (
    install_submit_single_attempt_safety,
)


SUBMIT_PATH = "/priapi/v1/nft/trading/seaport/step/submitOrder"


class _Settings:
    okx_api_base = "https://okx.test"


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
        return self.responses.pop(0)


class _MarketClient:
    def __init__(self, transport):
        self.transport = transport
        self.header_calls = 0

    def _build_headers(self, *, method, request_path, body):
        self.header_calls += 1
        return {"X-Test": f"{method}:{request_path}:{len(body)}"}


class _DummyClient:
    _SUBMIT_ORDER_PATH = SUBMIT_PATH

    def __init__(self, market):
        self.settings = _Settings()
        self.market = market
        self.original_calls = []

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

    def _complete_two_step_offer(
        self,
        step1_resp,
        private_key,
        chain_id,
        endpoint,
    ):
        _ = step1_resp, private_key, chain_id, endpoint
        return {"ok": True}


def _guarded_dummy(transport):
    class Client(_DummyClient):
        pass

    install_submit_single_attempt_safety(Client)
    return Client(_MarketClient(transport))


def _stdlib_transport(*responses):
    transport = StdlibHttpTransport(
        timeout=1,
        max_retries=4,
        rate_limit_per_sec=1000.0,
    )
    transport._rate_limiter = _NoWaitLimiter()
    transport._session = _Session(responses)
    return transport


def test_r52_layer_is_structurally_inside_r24_and_r25_wrappers():
    class Client(_DummyClient):
        pass

    install_submit_single_attempt_safety(Client)
    r52_request = Client._request
    assert getattr(r52_request, "_r52_submit_single_attempt_guard", False) is True

    install_submit_safety(Client)
    r24_request = Client._request
    assert getattr(r24_request, "_r24_priced_governor_guard", False) is True
    r24_nonlocals = inspect.getclosurevars(r24_request).nonlocals
    assert r24_nonlocals["original_request"] is r52_request

    install_receipt_safety(Client)
    r25_request = Client._request
    assert getattr(r25_request, "_r25_receipt_guard", False) is True
    assert r25_request.__wrapped__ is r24_request


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (429, OKXRateLimitError),
        (503, OKXSubmitError),
    ],
)
def test_submit_order_stdlib_transport_uses_exactly_one_http_attempt(status, error_type):
    transport = _stdlib_transport(
        _Response(status),
        _Response(200, {"code": "0", "data": {"successOrderIds": ["duplicate"]}}),
    )
    client = _guarded_dummy(transport)

    with pytest.raises(error_type):
        client._request(
            method="POST",
            path=f"{SUBMIT_PATH}?t=123",
            payload={"items": [{"protocolData": "{}"}]},
        )

    assert transport._session.calls == 1
    assert transport.max_retries == 4
    assert client.original_calls == []


def test_submit_order_transport_failure_is_not_retried_by_outer_request_loop():
    class FailingTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            raise RuntimeError("connection reset after send")

    transport = FailingTransport()
    client = _guarded_dummy(transport)

    with pytest.raises(OKXNetworkError, match="connection reset after send"):
        client._request(
            method="POST",
            path=SUBMIT_PATH,
            payload={"items": []},
        )

    assert transport.calls == 1
    assert client.original_calls == []


def test_non_submit_request_keeps_configured_transport_retry_policy(monkeypatch):
    transport = _stdlib_transport(
        _Response(429),
        _Response(200, {"code": "0", "data": {"items": []}}),
    )
    # Remove backoff sleep from this regression test; retry multiplicity is the
    # contract being asserted, not wall-clock timing.
    monkeypatch.setattr(
        StdlibHttpTransport,
        "_sleep_backoff",
        lambda self, attempt, headers: None,
    )
    client = _guarded_dummy(transport)

    result = client._request(
        method="GET",
        path="/api/v5/mktplace/nft/markets/offers",
        params={"limit": 1},
    )

    assert result["code"] == "0"
    assert transport._session.calls == 2
    assert transport.max_retries == 4
    assert len(client.original_calls) == 1


def test_explicit_submit_rejection_still_reaches_existing_receipt_contract():
    response = {
        "code": "0",
        "data": {
            "successOrderIds": [],
            "errors": [{"message": "This order is no longer valid"}],
        },
    }

    class ResultTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            return response

    class Client(_DummyClient):
        pass

    install_submit_single_attempt_safety(Client)
    install_receipt_safety(Client)
    transport = ResultTransport()
    client = Client(_MarketClient(transport))

    result = client._request(
        method="POST",
        path=SUBMIT_PATH,
        payload={"items": []},
    )

    assert result is response
    assert transport.calls == 1


def test_successful_submit_receipt_remains_accepted_after_single_attempt_layer():
    response = {
        "code": "0",
        "data": {"successOrderIds": ["order-r52"], "errors": []},
    }

    class ResultTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, **kwargs):
            self.calls += 1
            return response

    class Client(_DummyClient):
        pass

    install_submit_single_attempt_safety(Client)
    install_receipt_safety(Client)
    transport = ResultTransport()
    client = Client(_MarketClient(transport))

    result = client._request(
        method="POST",
        path=f"{SUBMIT_PATH}?t=999",
        payload={"items": []},
    )

    assert result is response
    assert transport.calls == 1
