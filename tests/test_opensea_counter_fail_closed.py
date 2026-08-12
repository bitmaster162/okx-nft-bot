from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient


WALLET = "0x" + "1" * 40


class _Transport:
    def __init__(self, *, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


def _client(*, response=None, exc=None):
    client = OpenSeaClient.__new__(OpenSeaClient)
    client.settings = SimpleNamespace()
    client.transport = _Transport(response=response, exc=exc)
    return client


def test_explicit_zero_counter_is_valid(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(response={"jsonrpc": "2.0", "id": 1, "result": "0x0"})

    assert client.get_seaport_counter(WALLET, "eth") == 0
    assert len(client.transport.calls) == 1


def test_positive_counter_is_parsed(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(response={"jsonrpc": "2.0", "id": 1, "result": "0x2a"})

    assert client.get_seaport_counter(WALLET, "eth") == 42


def test_missing_result_fails_closed(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(response={"jsonrpc": "2.0", "id": 1})

    with pytest.raises(RuntimeError, match="RPC response missing result"):
        client.get_seaport_counter(WALLET, "eth")


def test_rpc_error_fails_closed(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(
        response={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "upstream unavailable"},
        }
    )

    with pytest.raises(RuntimeError, match="RPC error"):
        client.get_seaport_counter(WALLET, "eth")


@pytest.mark.parametrize("result", [None, 0, "", "123", "not-hex", "0xzz"])
def test_malformed_counter_result_fails_closed(monkeypatch, result):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(response={"jsonrpc": "2.0", "id": 1, "result": result})

    with pytest.raises(RuntimeError, match="Failed to fetch counter from RPC"):
        client.get_seaport_counter(WALLET, "eth")


def test_transport_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    client = _client(exc=TimeoutError("rpc timeout"))

    with pytest.raises(RuntimeError, match="rpc timeout"):
        client.get_seaport_counter(WALLET, "eth")
