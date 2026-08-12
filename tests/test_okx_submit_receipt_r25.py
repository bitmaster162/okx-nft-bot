from __future__ import annotations

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.counterbid.receipt_safety import install_receipt_safety


def _make_client_class(response):
    class DummyClient:
        _SUBMIT_ORDER_PATH = "/priapi/v1/nft/trading/seaport/step/submitOrder"

        def __init__(self):
            self.calls = []

        def _request(self, *, method, path, params=None, payload=None):
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "params": params,
                    "payload": payload,
                }
            )
            return response

    install_receipt_safety(DummyClient)
    return DummyClient


def test_real_client_has_r25_receipt_guard_and_preserves_r24_marker():
    assert getattr(OKXAPIClient._request, "_r25_receipt_guard", False) is True
    assert getattr(OKXAPIClient._request, "_r24_priced_governor_guard", False) is True


def test_success_order_ids_is_accepted():
    response = {
        "code": "0",
        "data": {"successOrderIds": ["order-123"], "errors": []},
    }
    Client = _make_client_class(response)
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"items": []},
    )

    assert result is response
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", ["", "?", "pending", "none", "null", None])
def test_placeholder_success_order_id_is_rejected(value):
    response = {
        "code": "0",
        "data": {"successOrderIds": [value], "errors": []},
    }
    Client = _make_client_class(response)
    client = Client()

    with pytest.raises(Exception, match="receipt gate blocked: success response missing durable order id"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload={"items": []},
        )


def test_empty_success_without_errors_is_rejected():
    response = {
        "code": "0",
        "data": {"successOrderIds": [], "errors": []},
    }
    Client = _make_client_class(response)
    client = Client()

    with pytest.raises(Exception, match="success response missing durable order id"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload={"items": []},
        )


def test_nonzero_code_is_returned_for_existing_caller_error_handling():
    response = {"code": "51000", "msg": "bad request", "data": {}}
    Client = _make_client_class(response)
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"items": []},
    )

    assert result is response


def test_explicit_data_errors_are_returned_for_existing_retry_logic():
    response = {
        "code": "0",
        "data": {
            "successOrderIds": [],
            "errors": [{"message": "This order is no longer valid"}],
        },
    }
    Client = _make_client_class(response)
    client = Client()

    result = client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"items": []},
    )

    assert result is response


def test_top_level_fallback_order_id_is_accepted():
    response = {"code": "0", "orderId": "legacy-order-1", "data": {}}
    Client = _make_client_class(response)
    client = Client()

    assert client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"items": []},
    ) is response


def test_nested_fallback_offer_id_is_accepted():
    response = {"code": "0", "data": {"offerId": "legacy-offer-1"}}
    Client = _make_client_class(response)
    client = Client()

    assert client._request(
        method="POST",
        path=client._SUBMIT_ORDER_PATH,
        payload={"items": []},
    ) is response


def test_missing_receipt_preserves_no_longer_valid_message_for_outer_retry():
    response = {
        "code": "0",
        "msg": "This order is no longer valid",
        "data": {"successOrderIds": [], "errors": []},
    }
    Client = _make_client_class(response)
    client = Client()

    with pytest.raises(Exception, match="no longer valid"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload={"items": []},
        )


def test_non_submit_request_is_untouched():
    response = {"code": "0", "data": {}}
    Client = _make_client_class(response)
    client = Client()

    result = client._request(
        method="GET",
        path="/priapi/v1/nft/trading/offers",
        params={"limit": 1},
    )

    assert result is response


def test_non_object_submit_response_is_rejected():
    Client = _make_client_class(["unexpected"])
    client = Client()

    with pytest.raises(Exception, match="receipt gate blocked: response is not an object"):
        client._request(
            method="POST",
            path=client._SUBMIT_ORDER_PATH,
            payload={"items": []},
        )
