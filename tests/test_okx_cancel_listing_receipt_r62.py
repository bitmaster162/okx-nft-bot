from __future__ import annotations

import pytest

from okx_nft_bot.counterbid.cancel_listing_receipt_safety import (
    install_cancel_listing_receipt_safety,
)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _request(self, *, method, path, payload=None, params=None):
        self.calls.append({"method": method, "path": path, "payload": payload, "params": params})
        if not self._responses:
            raise AssertionError("unexpected extra request")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @staticmethod
    def _extract_scalar(response, *, keys):
        for key in keys:
            if key in response:
                return response[key]
        data = response.get("data")
        if isinstance(data, dict):
            for key in keys:
                if key in data:
                    return data[key]
        return None

    def cancel_listing(self, order_id):
        raise AssertionError("legacy cancel_listing must be replaced by R62 guard")


install_cancel_listing_receipt_safety(_FakeClient)


def test_code_zero_without_ack_fails_closed_single_request() -> None:
    client = _FakeClient([{"code": "0", "data": {}}])

    assert client.cancel_listing("order-1") is False
    assert len(client.calls) == 1
    assert client.calls[0]["payload"] == {"orderId": "order-1"}


def test_explicit_true_ack_confirms_cancel() -> None:
    client = _FakeClient([{"code": "0", "success": True}])

    assert client.cancel_listing("order-2") is True
    assert len(client.calls) == 1


def test_explicit_false_ack_fails_closed() -> None:
    client = _FakeClient([{"code": "0", "cancelled": False}])

    assert client.cancel_listing("order-3") is False
    assert len(client.calls) == 1


@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("0", False), ("false", False), ("failed", False)])
def test_legacy_scalar_semantics_are_preserved(value, expected) -> None:
    client = _FakeClient([{"code": "0", "result": value}])

    assert client.cancel_listing("order-4") is expected
    assert len(client.calls) == 1


def test_deterministic_nonzero_code_uses_legacy_offer_id_fallback_once() -> None:
    client = _FakeClient([
        {"code": "51000", "msg": "orderId shape rejected"},
        {"code": "0", "success": True},
    ])

    assert client.cancel_listing("order-5") is True
    assert [call["payload"] for call in client.calls] == [
        {"orderId": "order-5"},
        {"offerId": "order-5"},
    ]


def test_fallback_code_zero_without_ack_still_fails_closed() -> None:
    client = _FakeClient([
        {"code": "51000"},
        {"code": "0", "data": {}},
    ])

    assert client.cancel_listing("order-6") is False
    assert len(client.calls) == 2


def test_transport_exception_propagates_without_second_request() -> None:
    client = _FakeClient([RuntimeError("network ambiguous")])

    with pytest.raises(RuntimeError, match="network ambiguous"):
        client.cancel_listing("order-7")
    assert len(client.calls) == 1


def test_installer_is_idempotent() -> None:
    current = _FakeClient.cancel_listing
    install_cancel_listing_receipt_safety(_FakeClient)
    assert _FakeClient.cancel_listing is current
    assert getattr(_FakeClient.cancel_listing, "_r62_cancel_listing_receipt_guard", False) is True
