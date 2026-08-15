from __future__ import annotations

from okx_nft_bot.counterbid.cancel_effect_safety import install_cancel_effect_safety


class _Settings:
    okx_api_base = "https://okx.test"
    buyer_wallet_address = "0x00000000000000000000000000000000000000aa"


class _MarketClient:
    def __init__(self, transport):
        self.transport = transport

    def _build_headers(self, *, method, request_path, body):
        return {}


class _ResultTransport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def request_json(self, **kwargs):
        self.calls += 1
        return self.response


class _DummyClient:
    def __init__(self, response):
        self.settings = _Settings()
        self.market = _MarketClient(_ResultTransport(response))
        self.onchain_calls = []
        self.readback_calls = []

    def _market_client(self):
        return self.market

    def _request(self, *, method, path, params=None, payload=None):
        return self.market.transport.request_json(
            method=method,
            url=f"https://okx.test{path}",
            headers={},
            body="",
        )

    def cancel_offer(self, offer_id: str, chain: str = "bsc", order_params=None):
        raise AssertionError("legacy cancel_offer must be replaced")

    def _cancel_onchain_seaport(self, order_params, chain):
        self.onchain_calls.append((order_params, chain))
        return True

    def get_my_offers(self, chain="bsc", collection_address="", *, require_all_endpoints=False):
        self.readback_calls.append((chain, collection_address, require_all_endpoints))
        return []


def _client(response):
    class Client(_DummyClient):
        pass

    install_cancel_effect_safety(Client)
    return Client(response)


def test_code_zero_without_explicit_ack_is_not_cancel_success():
    client = _client({"code": "0", "data": {}})

    result = client.cancel_offer(
        "offer-r90",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert client.market.transport.calls == 1
    assert client.onchain_calls == []


def test_code_zero_with_explicit_false_ack_is_not_cancel_success():
    client = _client({"code": "0", "data": {"success": False}})

    result = client.cancel_offer(
        "offer-r90",
        order_params={"offerer": "0x1"},
    )

    assert result is False
    assert client.market.transport.calls == 1
    assert client.onchain_calls == []


def test_code_zero_with_explicit_true_ack_remains_success():
    client = _client({"code": "0", "data": {"success": True}})

    result = client.cancel_offer(
        "offer-r90",
        order_params={"offerer": "0x1"},
    )

    assert result is True
    assert client.market.transport.calls == 1
    assert client.onchain_calls == []
