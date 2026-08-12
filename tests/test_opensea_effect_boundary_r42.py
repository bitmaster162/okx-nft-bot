from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.sniper.opensea_effect_boundary_safety import (
    install_opensea_effect_boundary_safety,
)
from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT


OFFERER = "0x" + "1" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def _parameters(*, counter=7, amount=500, offer=None):
    return {
        "offerer": OFFERER,
        "counter": counter,
        "offer": offer
        if offer is not None
        else [
            {
                "itemType": 1,
                "token": WETH,
                "identifierOrCriteria": 0,
                "startAmount": str(amount),
                "endAmount": str(amount),
            }
        ],
    }


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client_class():
    class DummyClient:
        def __init__(self, *, current_counter=7, balance_response=None):
            self.current_counter = current_counter
            self.transport = _Transport(
                balance_response
                if balance_response is not None
                else {"jsonrpc": "2.0", "id": 1, "result": hex(500)}
            )
            self.counter_calls = []
            self.original_calls = []

        def get_seaport_counter(self, wallet_address, chain="eth"):
            self.counter_calls.append((wallet_address, chain))
            return self.current_counter

        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            self.original_calls.append((parameters, signature, chain))
            return {"order_id": "0xabc", "status": "submitted"}

    install_opensea_effect_boundary_safety(DummyClient)
    return DummyClient


@contextmanager
def _mirror_scope():
    token = _MIRROR_CONTEXT.set({"bidder": object(), "halted": False})
    try:
        yield
    finally:
        _MIRROR_CONTEXT.reset(token)


def test_r42_allows_fresh_counter_and_sufficient_balance(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    Client = _client_class()
    client = Client(current_counter=7)

    with _mirror_scope():
        result = client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert result["order_id"] == "0xabc"
    assert client.counter_calls == [(OFFERER, "eth")]
    assert len(client.transport.calls) == 1
    rpc_call = client.transport.calls[0]
    assert rpc_call["url"] == "https://rpc.test"
    body = json.loads(rpc_call["body"])
    assert body["method"] == "eth_call"
    assert body["params"][0]["to"] == WETH
    assert body["params"][0]["data"].startswith("0x70a08231")
    assert client.original_calls == [(_parameters(), "0xsig", "eth")]


def test_r42_blocks_stale_counter_before_balance_or_submit():
    Client = _client_class()
    client = Client(current_counter=8)

    with _mirror_scope(), pytest.raises(RuntimeError, match="stale Seaport counter"):
        client._submit_opensea_offer(_parameters(counter=7), "0xsig", "eth")

    assert client.transport.calls == []
    assert client.original_calls == []


def test_r42_blocks_insufficient_balance_before_submit():
    Client = _client_class()
    client = Client(
        current_counter=7,
        balance_response={"jsonrpc": "2.0", "id": 1, "result": hex(499)},
    )

    with _mirror_scope(), pytest.raises(RuntimeError, match="insufficient ERC20 balance"):
        client._submit_opensea_offer(_parameters(amount=500), "0xsig", "eth")

    assert len(client.transport.calls) == 1
    assert client.original_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": "not-hex"},
        {"error": {"code": -32000, "message": "rpc unavailable"}},
    ],
)
def test_r42_balance_uncertainty_fails_closed(response):
    Client = _client_class()
    client = Client(current_counter=7, balance_response=response)

    with _mirror_scope(), pytest.raises(RuntimeError, match="effect gate blocked"):
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert client.original_calls == []


def test_r42_sums_repeated_erc20_requirements():
    Client = _client_class()
    client = Client(
        current_counter=7,
        balance_response={"jsonrpc": "2.0", "id": 1, "result": hex(500)},
    )
    offer = [
        {
            "itemType": 1,
            "token": WETH,
            "identifierOrCriteria": 0,
            "startAmount": "300",
            "endAmount": "300",
        },
        {
            "itemType": 1,
            "token": WETH,
            "identifierOrCriteria": 0,
            "startAmount": "300",
            "endAmount": "300",
        },
    ]

    with _mirror_scope(), pytest.raises(RuntimeError, match="required=600 available=500"):
        client._submit_opensea_offer(_parameters(offer=offer), "0xsig", "eth")

    assert client.original_calls == []


def test_r42_rejects_non_erc20_offer_side():
    Client = _client_class()
    client = Client(current_counter=7)
    offer = [
        {
            "itemType": 2,
            "token": "0x" + "2" * 40,
            "identifierOrCriteria": 1,
            "startAmount": "1",
            "endAmount": "1",
        }
    ]

    with _mirror_scope(), pytest.raises(RuntimeError, match="only ERC20"):
        client._submit_opensea_offer(_parameters(offer=offer), "0xsig", "eth")

    assert client.counter_calls == []
    assert client.transport.calls == []
    assert client.original_calls == []


def test_r42_direct_non_mirror_client_call_is_unchanged():
    Client = _client_class()
    client = Client(current_counter=999)
    legacy_parameters = {"offerer": OFFERER, "offer": [{"startAmount": "1"}]}

    result = client._submit_opensea_offer(legacy_parameters, "0xsig", "eth")

    assert result["order_id"] == "0xabc"
    assert client.counter_calls == []
    assert client.transport.calls == []
    assert client.original_calls == [(legacy_parameters, "0xsig", "eth")]


def test_r42_installer_is_idempotent():
    Client = _client_class()
    first = Client._submit_opensea_offer
    install_opensea_effect_boundary_safety(Client)
    assert Client._submit_opensea_offer is first


def test_real_opensea_client_has_r41_then_r42_guards():
    current = OpenSeaClient._submit_opensea_offer
    assert getattr(current, "_r42_opensea_effect_boundary_guard", False)
    assert getattr(current.__wrapped__, "_r41_opensea_canonical_submit_route", False)
