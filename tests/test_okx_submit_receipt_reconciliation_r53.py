from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from okx_nft_bot.clients.http import HTTPStatusError
from okx_nft_bot.counterbid.inventory_safety import install_inventory_safety
from okx_nft_bot.counterbid.okx_api import (
    OKXAPIClient,
    OKXNetworkError,
    OKXRateLimitError,
    OKXSubmitError,
)
from okx_nft_bot.counterbid.receipt_reconciliation import (
    _seaport_order_hash,
    install_receipt_reconciliation,
)
from okx_nft_bot.counterbid.receipt_safety import install_receipt_safety


SUBMIT_PATH = "/priapi/v1/nft/trading/seaport/step/submitOrder"
V5_OFFERS = "/api/v5/mktplace/nft/markets/offers"
V5_COLLECTION_OFFERS = "/api/v5/mktplace/nft/markets/collection-offers"

# Golden vector from OKX's official Marketplace API "Query offer" response
# example. The endpoint publishes protocolData.parameters beside orderHash.
OFFICIAL_ORDER_HASH = (
    "0x48cc57480fdbe993821b6679910657845201351448bca623a6b7726fc1f7ff4b"
)
OFFICIAL_PARAMETERS = {
    "conduitKey": "0x618Cf13c76c1FFC2168fC47c98453dCc6134F5c8888888888888888888888888",
    "consideration": [
        {
            "endAmount": "1",
            "identifierOrCriteria": "77735144008553370296572895450686144694166639583550383356598452408298996120723",
            "itemType": 2,
            "recipient": "0x72fde15006cff1bfc1be596f03855a2c55b546e1",
            "startAmount": "1",
            "token": "0x457efd33def0bff2dfe33089d385898d919d3a10",
        }
    ],
    "counter": "0",
    "endTime": 1680307118,
    "offer": [
        {
            "endAmount": "110000",
            "identifierOrCriteria": "0",
            "itemType": 1,
            "startAmount": "110000",
            "token": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        }
    ],
    "offerer": "0x72fde15006cff1bfc1be596f03855a2c55b546e1",
    "orderType": 3,
    "salt": "1144075581",
    "startTime": 1680047927,
    "totalOriginalConsiderationItems": 1,
    "zone": "0x868B0635A8858dB9D984B5A27559f961Fd2736c0",
    "zoneHash": "0x0000000000000000000000000000000000000000000000000000000000000000",
}


def _matching_record(*, order_id="okx-order-123", order_hash=OFFICIAL_ORDER_HASH):
    return {
        "orderId": order_id,
        "orderHash": order_hash,
        "maker": OFFICIAL_PARAMETERS["offerer"],
        "status": "active",
        "collectionAddress": OFFICIAL_PARAMETERS["consideration"][0]["token"],
        "protocolData": {"parameters": OFFICIAL_PARAMETERS},
    }


def _make_client(mode: str, records=None, *, readback_error=None):
    class Client:
        _SUBMIT_ORDER_PATH = SUBMIT_PATH

        def __init__(self):
            self.settings = SimpleNamespace()
            self.submit_calls = 0
            self.readback_calls = []
            self.records = list(records or [])

        def _request(self, *, method, path, params=None, payload=None):
            canonical = str(path).split("?", 1)[0]
            if str(method).upper() == "POST" and canonical == SUBMIT_PATH:
                self.submit_calls += 1
                if mode == "network":
                    raise OKXNetworkError("connection reset after send")
                if mode == "rate":
                    raise OKXRateLimitError("HTTP 429: simulated")
                if mode == "503":
                    cause = HTTPStatusError(503, "upstream unavailable")
                    raise OKXSubmitError(str(cause)) from cause
                if mode == "400":
                    cause = HTTPStatusError(400, "bad request")
                    raise OKXSubmitError(str(cause)) from cause
                if mode == "missing":
                    return {"code": "0", "data": {"errors": []}}
                return {
                    "code": "0",
                    "data": {"successOrderIds": ["ordinary-success"], "errors": []},
                }
            return {"code": "0", "data": {}}

        def get_my_offers(
            self,
            chain="bsc",
            collection_address="",
            *,
            require_all_endpoints=False,
        ):
            self.readback_calls.append(
                (chain, collection_address, require_all_endpoints)
            )
            if readback_error is not None:
                raise readback_error
            return list(self.records)

        def _complete_two_step_offer(
            self,
            step1_resp,
            private_key,
            chain_id,
            endpoint,
        ):
            _ = step1_resp, private_key, endpoint
            payload = {
                "items": [
                    {
                        "protocolData": json.dumps(
                            {
                                "parameters": OFFICIAL_PARAMETERS,
                                "signature": "0x01",
                            }
                        )
                    }
                ]
            }
            return self._request(
                method="POST",
                path=SUBMIT_PATH + "?t=123",
                payload=payload,
            )

        def submit_seaport_order(
            self,
            *,
            chain,
            wallet_address,
            parameters,
            signature,
        ):
            _ = wallet_address, signature
            # Production direct payload intentionally drops counter from the
            # wire-level AdvancedOrder parameters. R53 must use method context.
            wire_parameters = dict(parameters)
            wire_parameters.pop("counter", None)
            payload = {
                "chain": 1 if chain == "eth" else 56,
                "items": [{"parameters": wire_parameters}],
            }
            return self._request(
                method="POST",
                path=SUBMIT_PATH + "?t=999",
                payload=payload,
            )

    return Client


def test_official_okx_query_offer_vector_matches_seaport_order_hash():
    assert _seaport_order_hash(OFFICIAL_PARAMETERS) == OFFICIAL_ORDER_HASH


def test_real_client_installs_r53_outside_strict_inventory_chain():
    assert getattr(OKXAPIClient._request, "_r53_receipt_reconcile_guard", False) is True
    assert getattr(
        OKXAPIClient._complete_two_step_offer,
        "_r53_receipt_reconcile_chain",
        False,
    ) is True
    assert getattr(
        OKXAPIClient.submit_seaport_order,
        "_r53_receipt_reconcile_parameters",
        False,
    ) is True
    assert getattr(OKXAPIClient._request, "_r38_strict_inventory_response_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r25_receipt_guard", False) is True


@pytest.mark.parametrize("mode", ["network", "rate", "503"])
def test_direct_ambiguous_submit_reconciles_exact_hash_without_second_post(mode):
    Client = _make_client(mode, [_matching_record()])
    install_receipt_reconciliation(Client)
    client = Client()

    result = client.submit_seaport_order(
        chain="eth",
        wallet_address=OFFICIAL_PARAMETERS["offerer"],
        parameters=OFFICIAL_PARAMETERS,
        signature="0x01",
    )

    assert result["r53_reconciled"] is True
    assert result["data"]["successOrderIds"] == ["okx-order-123"]
    assert result["data"]["reconciledOrderHash"] == OFFICIAL_ORDER_HASH
    assert client.submit_calls == 1
    assert client.readback_calls == [
        (
            "eth",
            OFFICIAL_PARAMETERS["consideration"][0]["token"].lower(),
            True,
        )
    ]


def test_two_step_protocoldata_reconciles_using_chain_context():
    Client = _make_client("network", [_matching_record()])
    install_receipt_reconciliation(Client)
    client = Client()

    result = client._complete_two_step_offer(
        {},
        "private-key",
        56,
        "/priapi/v1/nft/trading/createCollectionOffer?t=1",
    )

    assert result["data"]["successOrderIds"] == ["okx-order-123"]
    assert client.submit_calls == 1
    assert client.readback_calls[0][0] == "bsc"


def test_success_without_receipt_reconciles_outside_r25():
    Client = _make_client("missing", [_matching_record()])
    install_receipt_safety(Client)
    install_receipt_reconciliation(Client)
    client = Client()

    result = client.submit_seaport_order(
        chain="eth",
        wallet_address=OFFICIAL_PARAMETERS["offerer"],
        parameters=OFFICIAL_PARAMETERS,
        signature="0x01",
    )

    assert result["r53_reconciled"] is True
    assert result["data"]["successOrderIds"] == ["okx-order-123"]
    assert client.submit_calls == 1


def test_hash_mismatch_preserves_original_uncertainty():
    Client = _make_client(
        "network",
        [_matching_record(order_hash="0x" + "11" * 32)],
    )
    install_receipt_reconciliation(Client)
    client = Client()

    with pytest.raises(OKXNetworkError, match="connection reset after send"):
        client.submit_seaport_order(
            chain="eth",
            wallet_address=OFFICIAL_PARAMETERS["offerer"],
            parameters=OFFICIAL_PARAMETERS,
            signature="0x01",
        )

    assert client.submit_calls == 1
    assert len(client.readback_calls) == 1


def test_exact_hash_without_durable_order_id_preserves_uncertainty():
    Client = _make_client("network", [_matching_record(order_id="pending")])
    install_receipt_reconciliation(Client)
    client = Client()

    with pytest.raises(OKXNetworkError):
        client.submit_seaport_order(
            chain="eth",
            wallet_address=OFFICIAL_PARAMETERS["offerer"],
            parameters=OFFICIAL_PARAMETERS,
            signature="0x01",
        )

    assert client.submit_calls == 1


def test_readback_failure_preserves_original_uncertainty():
    Client = _make_client(
        "network",
        [_matching_record()],
        readback_error=RuntimeError("partial inventory failure"),
    )
    install_receipt_reconciliation(Client)
    client = Client()

    with pytest.raises(OKXNetworkError, match="connection reset after send"):
        client.submit_seaport_order(
            chain="eth",
            wallet_address=OFFICIAL_PARAMETERS["offerer"],
            parameters=OFFICIAL_PARAMETERS,
            signature="0x01",
        )

    assert client.submit_calls == 1


def test_deterministic_http_400_does_not_trigger_reconciliation():
    Client = _make_client("400", [_matching_record()])
    install_receipt_reconciliation(Client)
    client = Client()

    with pytest.raises(OKXSubmitError, match="HTTP 400"):
        client.submit_seaport_order(
            chain="eth",
            wallet_address=OFFICIAL_PARAMETERS["offerer"],
            parameters=OFFICIAL_PARAMETERS,
            signature="0x01",
        )

    assert client.submit_calls == 1
    assert client.readback_calls == []


def test_listing_shape_never_reconciles_through_offer_inventory():
    listing_parameters = {
        **OFFICIAL_PARAMETERS,
        "offer": [
            {
                "itemType": 2,
                "token": OFFICIAL_PARAMETERS["consideration"][0]["token"],
                "identifierOrCriteria": "1",
                "startAmount": "1",
                "endAmount": "1",
            }
        ],
        "consideration": [
            {
                "itemType": 1,
                "token": OFFICIAL_PARAMETERS["offer"][0]["token"],
                "identifierOrCriteria": "0",
                "startAmount": "110000",
                "endAmount": "110000",
                "recipient": OFFICIAL_PARAMETERS["offerer"],
            }
        ],
    }
    Client = _make_client("network", [_matching_record()])
    install_receipt_reconciliation(Client)
    client = Client()

    with pytest.raises(OKXNetworkError):
        client.submit_seaport_order(
            chain="eth",
            wallet_address=OFFICIAL_PARAMETERS["offerer"],
            parameters=listing_parameters,
            signature="0x01",
        )

    assert client.submit_calls == 1
    assert client.readback_calls == []


def test_installer_is_idempotent():
    Client = _make_client("network", [])
    install_receipt_reconciliation(Client)
    request_method = Client._request
    complete_method = Client._complete_two_step_offer
    direct_method = Client.submit_seaport_order

    install_receipt_reconciliation(Client)

    assert Client._request is request_method
    assert Client._complete_two_step_offer is complete_method
    assert Client.submit_seaport_order is direct_method


def test_current_v5_inventory_nonzero_code_fails_closed_in_strict_mode():
    responses = {
        V5_OFFERS: {"code": "50001", "msg": "token offers unavailable"},
        V5_COLLECTION_OFFERS: {
            "code": "0",
            "data": {"data": [_matching_record()]},
        },
    }

    class InventoryClient:
        def __init__(self):
            self.request_calls = []

        def _request(self, *, method, path, params=None, payload=None):
            _ = payload
            self.request_calls.append((method, path, params))
            return responses[path]

        def get_my_offers(
            self,
            chain="bsc",
            collection_address="",
            *,
            require_all_endpoints=False,
        ):
            endpoint_errors = []
            records = []
            for endpoint in (V5_OFFERS, V5_COLLECTION_OFFERS):
                try:
                    response = self._request(
                        method="GET",
                        path=endpoint,
                        params={
                            "chain": chain,
                            "collectionAddress": collection_address,
                        },
                    )
                except Exception as exc:
                    endpoint_errors.append(f"{endpoint}: {exc}")
                    continue
                if str(response.get("code", "0")) not in {"0", ""}:
                    continue
                data = response.get("data", {})
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    records.extend(data["data"])
            if endpoint_errors and require_all_endpoints:
                raise RuntimeError("partial endpoint failure: " + "; ".join(endpoint_errors))
            return records

    install_inventory_safety(InventoryClient)
    client = InventoryClient()

    with pytest.raises(RuntimeError, match="token offers unavailable"):
        client.get_my_offers(
            chain="eth",
            collection_address=OFFICIAL_PARAMETERS["consideration"][0]["token"],
            require_all_endpoints=True,
        )

    assert len(client.request_calls) == 2
