from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient, SEAPORT_ADDRESS_ETH
from okx_nft_bot.sniper.opensea_conduit_allowance_safety import (
    install_opensea_conduit_allowance_safety,
)
from okx_nft_bot.sniper.opensea_mirror_safety import _MIRROR_CONTEXT


OFFERER = "0x" + "1" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
CONDUIT = "0x" + "2" * 40
CONDUIT_KEY = "0x" + "3" * 64
CONTROLLER = "0x00000000f9490004c11cef243f5400493c00ad63"
ZERO_KEY = "0x" + "00" * 32


def _word(value: int) -> str:
    return f"{value:064x}"


def _conduit_result(conduit: str = CONDUIT, *, exists: bool = True) -> dict[str, str]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": "0x" + conduit[2:].zfill(64) + _word(1 if exists else 0),
    }


def _allowance_result(value: int) -> dict[str, str]:
    return {"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(value)}


def _parameters(*, amount: int = 500, conduit_key: str = CONDUIT_KEY, offer=None):
    return {
        "offerer": OFFERER,
        "counter": "7",
        "conduitKey": conduit_key,
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
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected RPC call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client_class(responses):
    class DummyClient:
        def __init__(self):
            self.transport = _Transport(responses)
            self.original_calls = []

        def _submit_opensea_offer(self, parameters, signature, chain="eth"):
            self.original_calls.append((parameters, signature, chain))
            return {"order_id": "0xabc", "status": "submitted"}

    install_opensea_conduit_allowance_safety(DummyClient)
    return DummyClient


@contextmanager
def _mirror_scope():
    token = _MIRROR_CONTEXT.set({"bidder": object(), "halted": False})
    try:
        yield
    finally:
        _MIRROR_CONTEXT.reset(token)


def test_r44_allows_sufficient_conduit_allowance(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.test")
    Client = _client_class([_conduit_result(), _allowance_result(500)])
    client = Client()

    with _mirror_scope():
        result = client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert result["order_id"] == "0xabc"
    assert client.original_calls == [(_parameters(), "0xsig", "eth")]
    assert len(client.transport.calls) == 2

    conduit_call = client.transport.calls[0]
    assert conduit_call["url"] == "https://rpc.test"
    conduit_body = json.loads(conduit_call["body"])
    assert conduit_body["method"] == "eth_call"
    assert conduit_body["params"][0]["to"] == CONTROLLER
    assert conduit_body["params"][0]["data"] == "0x6e9bfd9f" + CONDUIT_KEY[2:]

    allowance_body = json.loads(client.transport.calls[1]["body"])
    assert allowance_body["params"][0]["to"] == WETH
    calldata = allowance_body["params"][0]["data"]
    assert calldata.startswith("0xdd62ed3e")
    assert calldata[10:74] == OFFERER[2:].zfill(64)
    assert calldata[74:138] == CONDUIT[2:].zfill(64)


def test_r44_blocks_insufficient_conduit_allowance_before_submit():
    Client = _client_class([_conduit_result(), _allowance_result(499)])
    client = Client()

    with _mirror_scope(), pytest.raises(RuntimeError, match="insufficient ERC20 allowance"):
        client._submit_opensea_offer(_parameters(amount=500), "0xsig", "eth")

    assert len(client.transport.calls) == 2
    assert client.original_calls == []


def test_r44_blocks_missing_conduit_before_allowance_or_submit():
    Client = _client_class([_conduit_result(exists=False)])
    client = Client()

    with _mirror_scope(), pytest.raises(RuntimeError, match="conduit is not deployed"):
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert len(client.transport.calls) == 1
    assert client.original_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": "not-hex"},
        {"result": "0x1234"},
        {"error": {"code": -32000, "message": "rpc unavailable"}},
    ],
)
def test_r44_conduit_resolution_uncertainty_fails_closed(response):
    Client = _client_class([response])
    client = Client()

    with _mirror_scope(), pytest.raises(RuntimeError, match="allowance gate blocked"):
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert client.original_calls == []


def test_r44_zero_conduit_key_uses_seaport_directly():
    Client = _client_class([_allowance_result(500)])
    client = Client()

    with _mirror_scope():
        result = client._submit_opensea_offer(
            _parameters(conduit_key=ZERO_KEY), "0xsig", "eth"
        )

    assert result["status"] == "submitted"
    assert len(client.transport.calls) == 1
    body = json.loads(client.transport.calls[0]["body"])
    calldata = body["params"][0]["data"]
    assert body["params"][0]["to"] == WETH
    assert calldata.startswith("0xdd62ed3e")
    assert calldata[74:138] == SEAPORT_ADDRESS_ETH.lower()[2:].zfill(64)


def test_r44_sums_repeated_erc20_requirements_before_allowance_check():
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
    Client = _client_class([_conduit_result(), _allowance_result(599)])
    client = Client()

    with _mirror_scope(), pytest.raises(RuntimeError, match="required=600 allowance=599"):
        client._submit_opensea_offer(_parameters(offer=offer), "0xsig", "eth")

    assert client.original_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": "0x"},
        {"result": "0x01"},
        {"error": {"code": -32000, "message": "rpc unavailable"}},
    ],
)
def test_r44_allowance_uncertainty_fails_closed(response):
    Client = _client_class([_conduit_result(), response])
    client = Client()

    with _mirror_scope(), pytest.raises(RuntimeError, match="allowance gate blocked"):
        client._submit_opensea_offer(_parameters(), "0xsig", "eth")

    assert client.original_calls == []


def test_r44_direct_non_mirror_client_call_is_unchanged():
    Client = _client_class([])
    client = Client()
    legacy_parameters = {"offerer": OFFERER}

    result = client._submit_opensea_offer(legacy_parameters, "0xsig", "eth")

    assert result["order_id"] == "0xabc"
    assert client.transport.calls == []
    assert client.original_calls == [(legacy_parameters, "0xsig", "eth")]


def test_r44_installer_is_idempotent():
    Client = _client_class([])
    first = Client._submit_opensea_offer
    install_opensea_conduit_allowance_safety(Client)
    assert Client._submit_opensea_offer is first


def test_real_opensea_submit_wrapper_order_is_r43_r44_r42_r41():
    current = OpenSeaClient._submit_opensea_offer
    assert getattr(current, "_r43_opensea_receipt_reconciliation", False)
    current = current.__wrapped__
    assert getattr(current, "_r44_opensea_conduit_allowance_guard", False)
    current = current.__wrapped__
    assert getattr(current, "_r42_opensea_effect_boundary_guard", False)
    current = current.__wrapped__
    assert getattr(current, "_r41_opensea_canonical_submit_route", False)
