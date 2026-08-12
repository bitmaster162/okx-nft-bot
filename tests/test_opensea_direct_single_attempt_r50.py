from __future__ import annotations

import inspect

import pytest

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.clients.opensea import OpenSeaClient


OFFERER = "0x" + "1" * 40
RPC_URL = "https://rpc.test"


class _Settings:
    opensea_api_key = "test-key"
    opensea_api_base = "https://api.opensea.test/api"


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


def _base_submit():
    # Other safety suites install wrappers on OpenSeaClient at import time.
    # R50 hardens the underlying client method itself, so unwrap to test that
    # direct-client contract independently of wrapper import order.
    return inspect.unwrap(OpenSeaClient._submit_opensea_offer)


def _parameters():
    return {
        "offerer": OFFERER,
        "offer": [{"startAmount": "500"}],
    }


@pytest.mark.parametrize("status", [429, 503])
def test_r50_direct_stdlib_submit_never_retries_ambiguous_http_failure(monkeypatch, status):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    transport = _transport([
        _Response(status, text="ambiguous submit failure"),
        _Response(200, {"order_hash": "0xshould-not-be-used"}),
    ])
    client = OpenSeaClient(_Settings(), transport=transport)
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **kwargs: None)

    with pytest.raises(RuntimeError, match="Failed to submit offer to OpenSea"):
        _base_submit()(client, _parameters(), "0xsig", "eth")

    assert len(transport._session.calls) == 1
    assert transport.max_retries == 3


def test_r50_direct_stdlib_submit_success_keeps_normal_receipt_contract(monkeypatch):
    transport = _transport([
        _Response(200, {"order_hash": "0xorder"}),
    ])
    client = OpenSeaClient(_Settings(), transport=transport)
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **kwargs: None)

    result = _base_submit()(client, _parameters(), "0xsig", "eth")

    assert result["status"] == "submitted"
    assert result["order_id"] == "0xorder"
    assert result["offer_id"] == "0xorder"
    assert len(transport._session.calls) == 1
    assert transport.max_retries == 3


def test_r50_read_only_seaport_rpc_keeps_transport_retries(monkeypatch):
    monkeypatch.setattr(StdlibHttpTransport, "_sleep_backoff", lambda *args, **kwargs: None)
    monkeypatch.setenv("ETH_RPC_URL", RPC_URL)
    transport = _transport([
        _Response(503, text="temporary rpc failure"),
        _Response(200, {"result": "0x07"}),
    ])
    client = OpenSeaClient(_Settings(), transport=transport)

    assert client.get_seaport_counter(OFFERER, "eth") == 7
    assert len(transport._session.calls) == 2
    assert transport.max_retries == 3


def test_r50_custom_transport_contract_is_not_rewritten(monkeypatch):
    class CustomTransport:
        def __init__(self):
            self.calls = []

        def request_json(self, **kwargs):
            self.calls.append(kwargs)
            return {"order_hash": "0xcustom"}

    transport = CustomTransport()
    client = OpenSeaClient(_Settings(), transport=transport)
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **kwargs: None)

    result = _base_submit()(client, _parameters(), "0xsig", "eth")

    assert result["order_id"] == "0xcustom"
    assert len(transport.calls) == 1
