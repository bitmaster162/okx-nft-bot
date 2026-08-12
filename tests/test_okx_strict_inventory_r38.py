from __future__ import annotations

from types import SimpleNamespace

import pytest

from okx_nft_bot.counterbid.inventory_safety import install_inventory_safety
from okx_nft_bot.counterbid.okx_api import OKXAPIClient


TOKEN_PATH = "/priapi/v1/nft/trading/offer/token/list"
COLLECTION_PATH = "/priapi/v1/nft/trading/offer/collection/list"


def _make_client_class(responses):
    class FakeClient:
        def __init__(self):
            self.settings = SimpleNamespace()
            self.request_calls = []

        def _request(self, *, method, path, params=None, payload=None):
            self.request_calls.append((method, path, params, payload))
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value

        def get_my_offers(
            self,
            *,
            chain="bsc",
            limit=50,
            require_all_endpoints=False,
        ):
            endpoint_errors = []
            all_records = []
            for endpoint in (TOKEN_PATH, COLLECTION_PATH):
                try:
                    response = self._request(
                        method="GET",
                        path=endpoint,
                        params={"chain": chain, "limit": str(limit)},
                    )
                except Exception as exc:
                    endpoint_errors.append(f"{endpoint}: {exc}")
                    continue
                if str(response.get("code", "0")) not in ("0", ""):
                    continue
                data = response.get("data", {})
                if isinstance(data, dict):
                    records = data.get("records", [])
                    if isinstance(records, list):
                        all_records.extend(records)
            if endpoint_errors and (require_all_endpoints or not all_records):
                raise RuntimeError("get_my_offers endpoint failure: " + "; ".join(endpoint_errors))
            return all_records

    install_inventory_safety(FakeClient)
    return FakeClient


def test_real_client_has_r38_guard_and_preserves_prior_request_markers():
    assert getattr(OKXAPIClient._request, "_r38_strict_inventory_response_guard", False) is True
    assert getattr(OKXAPIClient.get_my_offers, "_r38_strict_inventory_context", False) is True
    assert getattr(OKXAPIClient._request, "_r25_receipt_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r24_priced_governor_guard", False) is True


def test_strict_inventory_both_nonzero_codes_fail_closed():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "50001", "msg": "token inventory unavailable"},
            COLLECTION_PATH: {"code": "50002", "msg": "collection inventory unavailable"},
        }
    )
    client = Client()

    with pytest.raises(RuntimeError, match="get_my_offers endpoint failure") as exc_info:
        client.get_my_offers(chain="eth", require_all_endpoints=True)

    text = str(exc_info.value)
    assert "token inventory unavailable" in text
    assert "collection inventory unavailable" in text
    assert len(client.request_calls) == 2


def test_strict_inventory_partial_semantic_failure_is_not_accepted():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "50001", "msg": "token inventory unavailable"},
            COLLECTION_PATH: {
                "code": "0",
                "data": {"records": [{"offerId": "known-offer"}]},
            },
        }
    )
    client = Client()

    with pytest.raises(RuntimeError, match="token inventory unavailable"):
        client.get_my_offers(chain="bsc", require_all_endpoints=True)


def test_non_strict_inventory_preserves_legacy_skip_behavior():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "50001", "msg": "token inventory unavailable"},
            COLLECTION_PATH: {"code": "50002", "msg": "collection inventory unavailable"},
        }
    )
    client = Client()

    assert client.get_my_offers(chain="eth", require_all_endpoints=False) == []
    assert len(client.request_calls) == 2


def test_strict_authoritative_empty_inventory_remains_allowed():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "0", "data": {"records": []}},
            COLLECTION_PATH: {"code": "", "data": {"records": []}},
        }
    )
    client = Client()

    assert client.get_my_offers(chain="eth", require_all_endpoints=True) == []


def test_strict_context_resets_after_failure():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "50001", "msg": "token inventory unavailable"},
            COLLECTION_PATH: {"code": "50002", "msg": "collection inventory unavailable"},
        }
    )
    client = Client()

    with pytest.raises(RuntimeError):
        client.get_my_offers(chain="eth", require_all_endpoints=True)

    direct = client._request(method="GET", path=TOKEN_PATH, params={})
    assert direct["code"] == "50001"


def test_non_inventory_request_is_untouched_even_inside_strict_context():
    other_path = "/priapi/v1/nft/trading/offer/cancelOrder"
    responses = {
        TOKEN_PATH: {"code": "0", "data": {"records": []}},
        COLLECTION_PATH: {"code": "0", "data": {"records": []}},
        other_path: {"code": "50099", "msg": "unrelated"},
    }
    Client = _make_client_class(responses)
    client = Client()

    assert client.get_my_offers(chain="eth", require_all_endpoints=True) == []
    assert client._request(method="GET", path=other_path, params={})["code"] == "50099"


def test_installer_is_idempotent():
    Client = _make_client_class(
        {
            TOKEN_PATH: {"code": "0", "data": {"records": []}},
            COLLECTION_PATH: {"code": "0", "data": {"records": []}},
        }
    )
    request_method = Client._request
    getter_method = Client.get_my_offers

    install_inventory_safety(Client)

    assert Client._request is request_method
    assert Client.get_my_offers is getter_method
