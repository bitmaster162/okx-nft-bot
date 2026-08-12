from __future__ import annotations

import inspect

import pytest

from okx_nft_bot.clients.http import HTTPStatusError
from okx_nft_bot.clients.opensea import OpenSeaClient, SEAPORT_ADDRESS_ETH


OFFERER = "0x" + "1" * 40
COLLECTION = "0x" + "2" * 40
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ZONE = "0x000056f7000000ece9003ca63978907a00ffd100"
CONDUIT = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
ZERO32 = "0x" + "00" * 32
PRIVATE_KEY = "0x" + "01" * 32


class _Settings:
    opensea_api_key = "test-key"
    opensea_api_base = "https://api.opensea.test/api"
    buyer_wallet_private_key = PRIVATE_KEY
    buyer_wallet_address = None


class _Transport:
    def __init__(self, *, post_result=None, post_error=None, get_result=None, get_error=None):
        self.post_result = post_result
        self.post_error = post_error
        self.get_result = get_result
        self.get_error = get_error
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        method = kwargs["method"].upper()
        if method == "POST":
            if self.post_error is not None:
                raise self.post_error
            return self.post_result if self.post_result is not None else {"order_hash": "0xnormal"}
        if method == "GET":
            if self.get_error is not None:
                raise self.get_error
            return self.get_result if self.get_result is not None else {}
        raise AssertionError(f"unexpected method {method}")


def _base_submit():
    return inspect.unwrap(OpenSeaClient._submit_opensea_offer)


def _parameters(*, salt: int = 123):
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
        "salt": str(salt),
        "conduitKey": CONDUIT,
        "counter": "7",
        "totalOriginalConsiderationItems": 1,
    }


def _client(transport, monkeypatch):
    client = OpenSeaClient(_Settings(), transport=transport)
    monkeypatch.setattr(client, "_live_submit_block_reason", lambda **kwargs: None)
    return client


def test_r51_direct_ambiguous_transport_failure_reconciles_exact_order_hash(monkeypatch):
    transport = _Transport(post_error=RuntimeError("connection reset after send"))
    client = _client(transport, monkeypatch)
    expected = client._derive_seaport_order_hash(_parameters())
    transport.get_result = {"order_hash": expected, "order_status": "active"}

    result = _base_submit()(client, _parameters(), "0xsig", "eth")

    assert result["status"] == "submitted"
    assert result["reconciled"] is True
    assert result["order_id"] == expected
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1]["url"].endswith(
        f"/api/v2/orders/chain/ethereum/protocol/{SEAPORT_ADDRESS_ETH}/{expected}"
    )


def test_r51_direct_unresolved_ambiguous_failure_returns_uncertain(monkeypatch):
    transport = _Transport(
        post_error=HTTPStatusError(503, "upstream unavailable"),
        get_error=HTTPStatusError(404, "not indexed"),
    )
    client = _client(transport, monkeypatch)
    expected = client._derive_seaport_order_hash(_parameters())

    result = _base_submit()(client, _parameters(), "0xsig", "eth")

    assert result["status"] == "submit_uncertain"
    assert result["receipt_uncertain"] is True
    assert result["order_hash"] == expected
    assert result["order_id"] == expected
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_r51_direct_success_without_durable_id_is_reconciled(monkeypatch):
    transport = _Transport(post_result={"status": "accepted"})
    client = _client(transport, monkeypatch)
    expected = client._derive_seaport_order_hash(_parameters())
    transport.get_result = {"orderHash": expected, "order_status": "active"}

    result = _base_submit()(client, _parameters(), "0xsig", "eth")

    assert result["status"] == "submitted"
    assert result["reconciled"] is True
    assert result["order_hash"] == expected
    assert [call["method"] for call in transport.calls] == ["POST", "GET"]


def test_r51_direct_deterministic_400_is_not_reconciled(monkeypatch):
    transport = _Transport(post_error=HTTPStatusError(400, "invalid order"))
    client = _client(transport, monkeypatch)

    with pytest.raises(RuntimeError, match="Failed to submit offer to OpenSea") as exc_info:
        _base_submit()(client, _parameters(), "0xsig", "eth")

    assert isinstance(exc_info.value.__cause__, HTTPStatusError)
    assert exc_info.value.__cause__.status == 400
    assert [call["method"] for call in transport.calls] == ["POST"]


def test_r51_partial_private_submit_keeps_legacy_failure_without_readback(monkeypatch):
    transport = _Transport(post_result={"status": "accepted"})
    client = _client(transport, monkeypatch)
    partial = {
        "offerer": OFFERER,
        "offer": [{"startAmount": "500"}],
    }

    with pytest.raises(
        RuntimeError,
        match="Failed to submit offer to OpenSea: OpenSea submit response missing order id",
    ):
        _base_submit()(client, partial, "0xsig", "eth")

    assert [call["method"] for call in transport.calls] == ["POST"]


def test_r51_public_create_hash_preflight_blocks_before_submit(monkeypatch):
    transport = _Transport()
    client = _client(transport, monkeypatch)
    malformed = {
        "offerer": OFFERER,
        "offer": [{"startAmount": "500"}],
    }
    submit_calls = []

    monkeypatch.setattr(client, "get_seaport_counter", lambda *args, **kwargs: 7)
    monkeypatch.setattr(client, "_build_seaport_offer", lambda **kwargs: malformed)
    monkeypatch.setattr(client, "_sign_seaport_order", lambda *args, **kwargs: "0xsig")
    monkeypatch.setattr(
        client,
        "_submit_opensea_offer",
        lambda *args, **kwargs: submit_calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="deterministic order hash unavailable"):
        client.create_opensea_offer(
            chain="eth",
            collection_address=COLLECTION,
            token_id=42,
            price_wei=500,
            currency_address=WETH,
            private_key=PRIVATE_KEY,
        )

    assert submit_calls == []
    assert transport.calls == []


def test_r51_order_hash_is_stable_and_signed_fields_change_it(monkeypatch):
    client = _client(_Transport(), monkeypatch)
    first = client._derive_seaport_order_hash(_parameters())
    second = client._derive_seaport_order_hash(_parameters())
    changed = client._derive_seaport_order_hash(_parameters(salt=124))

    assert first == second
    assert first.startswith("0x") and len(first) == 66
    assert changed != first
