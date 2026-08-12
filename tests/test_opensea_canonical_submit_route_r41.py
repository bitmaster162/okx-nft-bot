from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.sniper.opensea_submit_route_safety import (
    canonical_opensea_api_base,
    install_opensea_submit_route_safety,
)


PARAMETERS = {
    "offerer": "0x" + "1" * 40,
    "offer": [{"startAmount": "1000000000000000"}],
}


class _Transport:
    def __init__(self):
        self.calls = []

    def request_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"order_hash": "0xabc123"}


def _client(base: str):
    client = OpenSeaClient.__new__(OpenSeaClient)
    client.settings = SimpleNamespace(
        opensea_api_key="test-key",
        opensea_api_base=base,
    )
    client.transport = _Transport()
    client._live_submit_block_reason = lambda **_kwargs: None
    return client


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://api.opensea.io", "https://api.opensea.io/api"),
        ("https://api.opensea.io/", "https://api.opensea.io/api"),
        ("https://api.opensea.io/api", "https://api.opensea.io/api"),
        ("https://proxy.test/opensea", "https://proxy.test/opensea/api"),
    ],
)
def test_canonical_api_base_supports_root_and_existing_api(base, expected):
    assert canonical_opensea_api_base(base) == expected


def test_real_open_sea_submit_uses_canonical_route_without_mutating_settings():
    client = _client("https://api.opensea.test")
    original_settings = client.settings

    result = client._submit_opensea_offer(PARAMETERS, "0xsig", "ethereum")

    assert result["order_id"] == "0xabc123"
    assert len(client.transport.calls) == 1
    assert client.transport.calls[0]["url"] == (
        "https://api.opensea.test/api/v2/orders/ethereum/seaport/offers"
    )
    assert client.settings is original_settings
    assert client.settings.opensea_api_base == "https://api.opensea.test"


def test_existing_api_suffix_does_not_duplicate_api_segment():
    client = _client("https://api.opensea.test/api/")

    client._submit_opensea_offer(PARAMETERS, "0xsig", "eth")

    assert client.transport.calls[0]["url"] == (
        "https://api.opensea.test/api/v2/orders/ethereum/seaport/offers"
    )
    assert client.settings.opensea_api_base == "https://api.opensea.test/api/"


@pytest.mark.parametrize(
    "bad_base",
    [
        "",
        "api.opensea.io",
        "ftp://api.opensea.io",
        "https://api.opensea.io?token=bad",
        "https://api.opensea.io/#fragment",
    ],
)
def test_invalid_base_fails_before_effectful_submit(bad_base):
    client = _client(bad_base)

    with pytest.raises(RuntimeError, match="OpenSea API base"):
        client._submit_opensea_offer(PARAMETERS, "0xsig", "eth")

    assert client.transport.calls == []


def test_real_class_has_r41_route_guard_installed():
    assert getattr(
        OpenSeaClient._submit_opensea_offer,
        "_r41_opensea_canonical_submit_route",
        False,
    ) is True


def test_installer_is_idempotent():
    current = OpenSeaClient._submit_opensea_offer

    install_opensea_submit_route_safety(OpenSeaClient)

    assert OpenSeaClient._submit_opensea_offer is current
