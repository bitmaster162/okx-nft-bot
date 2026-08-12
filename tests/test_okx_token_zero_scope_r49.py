from __future__ import annotations

from types import SimpleNamespace

import pytest

import okx_nft_bot.counterbid.okx_api as okx_api
from okx_nft_bot.counterbid.okx_api import OKXAPIClient, OKXSubmitError


PRIVATE_KEY = "0x" + "11" * 32
COLLECTION = "0x" + "22" * 20
CURRENCY = "0x" + "33" * 20


def _client() -> OKXAPIClient:
    client = object.__new__(OKXAPIClient)
    client.settings = SimpleNamespace(buyer_wallet_private_key=PRIVATE_KEY)
    client._last_onchain_cancel_ts = 0.0
    return client


def _call_create_offer(client: OKXAPIClient, token_id):
    return client.create_offer(
        chain="eth",
        wallet_address="0x" + "44" * 20,
        collection_address=COLLECTION,
        token_id=token_id,
        price_raw="1000000000000000000",
        currency_address=CURRENCY,
        valid_time=2_000_000_000,
        project=123,
    )


@pytest.mark.parametrize("token_id", [0, "0"])
def test_r49_primary_zero_uses_item_offer_route(monkeypatch, token_id):
    client = _client()
    requests: list[tuple[str, dict]] = []

    def fake_request(*, method, path, params=None, payload=None):
        requests.append((path, payload))
        return {"code": "0", "data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(
        client,
        "_complete_two_step_offer",
        lambda step1_resp, private_key, chain_id, endpoint: {"status": "captured"},
    )

    result = _call_create_offer(client, token_id)

    assert result == {"status": "captured"}
    assert len(requests) == 1
    path, payload = requests[0]
    assert path.startswith(client._CREATE_OFFER_PATH + "?t=")
    assert not path.startswith(client._CREATE_COLLECTION_OFFER_PATH + "?t=")
    item = payload["items"][0]
    assert item["tokenId"] == "0"
    assert item["collectionAddress"] == COLLECTION


@pytest.mark.parametrize("token_id", [None, ""])
def test_r49_explicit_absence_stays_collection_offer(monkeypatch, token_id):
    client = _client()
    requests: list[tuple[str, dict]] = []

    def fake_request(*, method, path, params=None, payload=None):
        requests.append((path, payload))
        return {"code": "0", "data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(
        client,
        "_complete_two_step_offer",
        lambda step1_resp, private_key, chain_id, endpoint: {"status": "captured"},
    )

    _call_create_offer(client, token_id)

    path, payload = requests[0]
    assert path.startswith(client._CREATE_COLLECTION_OFFER_PATH + "?t=")
    item = payload["items"][0]
    assert "tokenId" not in item
    assert "collectionAddress" not in item


def test_r49_nonzero_item_offer_semantics_are_unchanged(monkeypatch):
    client = _client()
    requests: list[tuple[str, dict]] = []

    def fake_request(*, method, path, params=None, payload=None):
        requests.append((path, payload))
        return {"code": "0", "data": {}}

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(
        client,
        "_complete_two_step_offer",
        lambda step1_resp, private_key, chain_id, endpoint: {"status": "captured"},
    )

    _call_create_offer(client, 7)

    path, payload = requests[0]
    assert path.startswith(client._CREATE_OFFER_PATH + "?t=")
    assert payload["items"][0]["tokenId"] == "7"
    assert payload["items"][0]["collectionAddress"] == COLLECTION


def test_r49_primary_path_preserves_integer_zero_into_direct_fallback(monkeypatch):
    client = _client()
    direct_calls: list[dict] = []

    monkeypatch.setattr(
        client,
        "_request",
        lambda *, method, path, params=None, payload=None: {"code": "0", "data": {}},
    )

    def reject_step2(step1_resp, private_key, chain_id, endpoint):
        raise OKXSubmitError("This order is no longer valid")

    def capture_direct(**kwargs):
        direct_calls.append(kwargs)
        return {"status": "direct"}

    monkeypatch.setattr(client, "_complete_two_step_offer", reject_step2)
    monkeypatch.setattr(client, "_create_offer_direct", capture_direct)
    monkeypatch.setattr(okx_api.time, "sleep", lambda _seconds: None)

    result = _call_create_offer(client, 0)

    assert result == {"status": "direct"}
    assert len(direct_calls) == 1
    assert direct_calls[0]["token_id"] == 0


def test_r49_direct_fallback_zero_uses_per_item_builder(monkeypatch):
    client = _client()
    account = SimpleNamespace(address="0x" + "55" * 20)
    built: list[tuple[str, dict]] = []
    submitted: list[dict] = []

    import okx_nft_bot.execution_governor as governor_module
    import okx_nft_bot.signing.seaport_signer as signer_module

    class FakeGovernor:
        def __init__(self, *args, **kwargs):
            pass

        def allocate_seaport_counter(self, wallet, chain):
            return 9

    def fake_collection_builder(**kwargs):
        built.append(("collection", kwargs))
        return {"kind": "collection"}

    def fake_item_builder(**kwargs):
        built.append(("item", kwargs))
        return {"kind": "item", "token_id": kwargs["token_id"]}

    monkeypatch.setattr(governor_module, "ExecutionGovernor", FakeGovernor)
    monkeypatch.setattr(signer_module, "build_order_payload", fake_collection_builder)
    monkeypatch.setattr(signer_module, "build_per_item_offer", fake_item_builder)
    monkeypatch.setattr(signer_module, "sign_order", lambda parameters, private_key, chain_id: "0xsig")

    def fake_submit(**kwargs):
        submitted.append(kwargs)
        return {"status": "submitted"}

    monkeypatch.setattr(client, "submit_seaport_order", fake_submit)

    result = client._create_offer_direct(
        chain="eth",
        chain_id=1,
        account=account,
        private_key=PRIVATE_KEY,
        collection_address=COLLECTION,
        token_id=0,
        price_raw="1000000000000000000",
        currency_address=CURRENCY,
        valid_time=2_000_000_000,
    )

    assert result == {"status": "submitted"}
    assert [kind for kind, _kwargs in built] == ["item"]
    assert built[0][1]["token_id"] == 0
    assert submitted[0]["parameters"] == {"kind": "item", "token_id": 0}


def test_r49_direct_fallback_blank_stays_collection_builder(monkeypatch):
    client = _client()
    account = SimpleNamespace(address="0x" + "55" * 20)
    built: list[str] = []

    import okx_nft_bot.execution_governor as governor_module
    import okx_nft_bot.signing.seaport_signer as signer_module

    class FakeGovernor:
        def __init__(self, *args, **kwargs):
            pass

        def allocate_seaport_counter(self, wallet, chain):
            return 9

    def fake_collection_builder(**kwargs):
        built.append("collection")
        return {"kind": "collection"}

    def fake_item_builder(**kwargs):
        built.append("item")
        return {"kind": "item"}

    monkeypatch.setattr(governor_module, "ExecutionGovernor", FakeGovernor)
    monkeypatch.setattr(signer_module, "build_order_payload", fake_collection_builder)
    monkeypatch.setattr(signer_module, "build_per_item_offer", fake_item_builder)
    monkeypatch.setattr(signer_module, "sign_order", lambda parameters, private_key, chain_id: "0xsig")
    monkeypatch.setattr(
        client,
        "submit_seaport_order",
        lambda **kwargs: {"status": "submitted"},
    )

    client._create_offer_direct(
        chain="eth",
        chain_id=1,
        account=account,
        private_key=PRIVATE_KEY,
        collection_address=COLLECTION,
        token_id="",
        price_raw="1000000000000000000",
        currency_address=CURRENCY,
        valid_time=2_000_000_000,
    )

    assert built == ["collection"]
