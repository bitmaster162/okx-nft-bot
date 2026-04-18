from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from okx_nft_bot.clients.http import HTTPStatusError
from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.okx_api import (
    OKXAPIClient,
    OKXAuthError,
    OKXNetworkError,
    OKXRateLimitError,
    _sanitize_payload_for_log,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        okx_api_key="key",
        okx_api_secret="secret",
        okx_api_passphrase="passphrase",
        buyer_wallet_address="0xbuyer",
    )


class FakeTransport:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response or {"code": "0", "data": []}
        self.error = error
        self.calls: list[dict[str, object]] = []

    def request_json(self, *, method, url, headers, body=""):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if self.error:
            raise self.error
        return self.response


class FakeMarketClient:
    def __init__(self, *, settings: Settings, transport: FakeTransport) -> None:
        self.settings = settings
        self.transport = transport
        self.headers_calls: list[dict[str, str]] = []

    def _build_headers(self, *, method: str, request_path: str, body: str) -> dict[str, str]:
        self.headers_calls.append({"method": method, "request_path": request_path, "body": body})
        return {"X-Test": "1"}


def test_submit_offer_calls_request_and_returns_offer_metadata(tmp_path: Path) -> None:
    client = OKXAPIClient(settings=_settings(tmp_path))
    with patch.object(
        client,
        "submit_seaport_order",
        return_value={"offer_id": "offer-1", "status": "open"},
    ) as submit_mock:
        result = client.submit_offer({"parameters": {"foo": "bar"}, "signature": "0xabc"})

    submit_mock.assert_called_once_with(
        chain="bsc",
        wallet_address="",
        parameters={"foo": "bar"},
        signature="0xabc",
    )
    assert result["offer_id"] == "offer-1"
    assert result["status"] == "open"


def test_cancel_offer_calls_request_and_returns_true_on_success(tmp_path: Path) -> None:
    client = OKXAPIClient(settings=_settings(tmp_path))
    with patch.object(client, "_cancel_via_api", return_value=True) as cancel_api_mock, patch.object(
        client,
        "_cancel_onchain_seaport",
    ) as cancel_onchain_mock:
        result = client.cancel_offer("offer-1")

    cancel_api_mock.assert_called_once_with("offer-1", "bsc")
    cancel_onchain_mock.assert_not_called()
    assert result is True


def test_get_my_offers_uses_request_with_buyer_wallet_filter(tmp_path: Path) -> None:
    client = OKXAPIClient(settings=_settings(tmp_path))
    with patch.object(
        client,
        "_request",
        side_effect=[
            {"code": "0", "data": [{"orderId": "offer-1"}]},
            {"code": "0", "data": [{"orderId": "offer-2"}]},
        ],
    ) as request_mock:
        offers = client.get_my_offers(chain="bsc")

    assert request_mock.call_count == 2
    assert request_mock.call_args_list[0].kwargs == {
        "method": "GET",
        "path": "/api/v5/mktplace/nft/markets/offers",
        "params": {
            "chain": "bsc",
            "maker": "0xbuyer",
            "status": "active",
            "limit": client.settings.okx_page_limit,
        },
    }
    assert request_mock.call_args_list[1].kwargs == {
        "method": "GET",
        "path": "/api/v5/mktplace/nft/markets/collection-offers",
        "params": {
            "chain": "bsc",
            "maker": "0xbuyer",
            "status": "active",
            "limit": client.settings.okx_page_limit,
        },
    }
    assert offers == [{"orderId": "offer-1"}, {"orderId": "offer-2"}]


def test_request_maps_429_to_rate_limit_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    transport = FakeTransport(error=HTTPStatusError(status=429, body="slow down"))
    market_client = FakeMarketClient(settings=settings, transport=transport)
    client = OKXAPIClient(settings=settings, market_client=market_client)

    with pytest.raises(OKXRateLimitError):
        client._request(method="POST", path="/api/v5/mktplace/nft/markets/offers", payload={"x": 1})


def test_request_maps_auth_and_network_errors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    auth_transport = FakeTransport(error=HTTPStatusError(status=401, body="forbidden"))
    auth_client = OKXAPIClient(
        settings=settings,
        market_client=FakeMarketClient(settings=settings, transport=auth_transport),
    )
    with pytest.raises(OKXAuthError):
        auth_client._request(method="GET", path="/api/v5/mktplace/nft/markets/offers")

    net_transport = FakeTransport(error=RuntimeError("socket closed"))
    net_client = OKXAPIClient(
        settings=settings,
        market_client=FakeMarketClient(settings=settings, transport=net_transport),
    )
    with pytest.raises(OKXNetworkError):
        net_client._request(method="GET", path="/api/v5/mktplace/nft/markets/offers")


def test_sanitize_payload_for_log_redacts_nested_signatures() -> None:
    payload = {
        "signature": "0x" + "a" * 130,
        "protocolData": {
            "parameters": {"offerer": "0xbuyer"},
            "signature": "0x" + "b" * 130,
        },
        "items": [
            {
                "protocolData": '{"parameters": {"foo": "bar"}, "signature": "0x' + ('c' * 130) + '"}',
            }
        ],
        "buyer_wallet_private_key": "0xsecret",
    }

    sanitized = _sanitize_payload_for_log(payload)

    assert sanitized["signature"] != payload["signature"]
    assert sanitized["protocolData"]["signature"] != payload["protocolData"]["signature"]
    assert sanitized["items"][0]["protocolData"]["signature"] != ("0x" + "c" * 130)
    assert sanitized["buyer_wallet_private_key"] == "***REDACTED***"
    assert sanitized["protocolData"]["parameters"]["offerer"] == "0xbuyer"
