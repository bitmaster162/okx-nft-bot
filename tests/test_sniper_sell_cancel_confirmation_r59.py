from __future__ import annotations

import inspect

import pytest

from okx_nft_bot.sniper.counter_bidder import CounterBidder
from okx_nft_bot.sniper.sell_cancel_confirmation_safety import (
    SellCancelNotConfirmed,
    _SellCancelConfirmationProxy,
    install_sell_cancel_confirmation_safety,
)


class _FakeClient:
    def __init__(self, cancel_result=True, cancel_exc: Exception | None = None):
        self.cancel_result = cancel_result
        self.cancel_exc = cancel_exc
        self.cancel_calls: list[tuple[tuple, dict]] = []
        self.create_calls: list[tuple[tuple, dict]] = []
        self.read_calls: list[tuple[str, tuple, dict]] = []

    def cancel_listing(self, *args, **kwargs):
        self.cancel_calls.append((args, kwargs))
        if self.cancel_exc is not None:
            raise self.cancel_exc
        return self.cancel_result

    def create_listing(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))
        return {"status": "delegated"}

    def get_wallet_nfts(self, *args, **kwargs):
        self.read_calls.append(("get_wallet_nfts", args, kwargs))
        return {
            "data": {
                "data": [
                    {"tokenId": 0, "name": "zero"},
                    {"tokenId": "7", "name": "seven"},
                ]
            }
        }

    def get_listings(self, *args, **kwargs):
        self.read_calls.append(("get_listings", args, kwargs))
        return {
            "data": {
                "data": [
                    {"tokenId": 0, "orderId": "old-zero"},
                ]
            }
        }


@pytest.mark.parametrize("result", [False, None, 0, 1, "true", "0", object()])
def test_non_literal_true_cancel_result_fails_closed(result):
    raw = _FakeClient(cancel_result=result)
    proxy = _SellCancelConfirmationProxy(raw)

    with pytest.raises(SellCancelNotConfirmed):
        proxy.cancel_listing("order-123")

    assert raw.cancel_calls == [(('order-123',), {})]


def test_literal_true_cancel_result_is_the_only_success():
    raw = _FakeClient(cancel_result=True)
    proxy = _SellCancelConfirmationProxy(raw)

    assert proxy.cancel_listing("order-123") is True
    assert raw.cancel_calls == [(('order-123',), {})]


def test_cancel_exception_propagates_without_second_effect_attempt():
    ambiguous = RuntimeError("ambiguous transport receipt")
    raw = _FakeClient(cancel_exc=ambiguous)
    proxy = _SellCancelConfirmationProxy(raw)

    with pytest.raises(RuntimeError, match="ambiguous transport receipt") as caught:
        proxy.cancel_listing("order-ambiguous")

    assert caught.value is ambiguous
    assert raw.cancel_calls == [(('order-ambiguous',), {})]


def test_keyword_order_id_is_reported_but_not_retried():
    raw = _FakeClient(cancel_result=False)
    proxy = _SellCancelConfirmationProxy(raw)

    with pytest.raises(SellCancelNotConfirmed, match="order-keyword"):
        proxy.cancel_listing(order_id="order-keyword")

    assert raw.cancel_calls == [((), {"order_id": "order-keyword"})]


def test_non_cancel_effect_methods_delegate_unchanged():
    raw = _FakeClient(cancel_result=True)
    proxy = _SellCancelConfirmationProxy(raw)

    result = proxy.create_listing(token_id="0", price_raw="123")

    assert result == {"status": "delegated"}
    assert raw.create_calls == [((), {"token_id": "0", "price_raw": "123"})]
    assert raw.cancel_calls == []


def test_installer_is_idempotent_and_preserves_none_client():
    class DummyBidder:
        def __init__(self, client):
            self.client = client

        def _get_okx_client(self):
            return self.client

    install_sell_cancel_confirmation_safety(DummyBidder)
    first = DummyBidder._get_okx_client
    install_sell_cancel_confirmation_safety(DummyBidder)
    second = DummyBidder._get_okx_client

    assert first is second
    assert getattr(second, "_r59_sell_cancel_confirmation", False) is True
    assert DummyBidder(None)._get_okx_client() is None


def test_installer_wraps_client_and_enforces_confirmation():
    class DummyBidder:
        def __init__(self, client):
            self.client = client

        def _get_okx_client(self):
            return self.client

    raw = _FakeClient(cancel_result=False)
    install_sell_cancel_confirmation_safety(DummyBidder)
    proxy = DummyBidder(raw)._get_okx_client()

    assert isinstance(proxy, _SellCancelConfirmationProxy)
    with pytest.raises(SellCancelNotConfirmed):
        proxy.cancel_listing("order-false")
    assert raw.cancel_calls == [(('order-false',), {})]


def test_package_installer_stacks_after_r58_token_zero_read_guard():
    raw = _FakeClient(cancel_result=False)
    bidder = CounterBidder.__new__(CounterBidder)
    bidder._okx_client = raw

    client = bidder._get_okx_client()

    # R58 still normalizes numeric token zero on both sell-side reads.
    inventory = client.get_wallet_nfts(chain="bsc", wallet_address="0xabc")
    listings = client.get_listings(chain="bsc", collection_address="0xdef")
    assert inventory["data"]["data"][0]["tokenId"] == "0"
    assert listings["data"]["data"][0]["tokenId"] == "0"

    # R59 sits outside that read proxy and converts False into the exception
    # that the legacy sell loop already handles with `continue`.
    with pytest.raises(SellCancelNotConfirmed):
        client.cancel_listing("old-zero")
    assert raw.cancel_calls == [(('old-zero',), {})]

    accessor = CounterBidder._get_okx_client
    assert getattr(accessor, "_r58_sell_token_zero_scope", False) is True
    assert getattr(accessor, "_r59_sell_cancel_confirmation", False) is True


def test_package_guard_delegates_confirmed_cancel_once():
    raw = _FakeClient(cancel_result=True)
    bidder = CounterBidder.__new__(CounterBidder)
    bidder._okx_client = raw

    client = bidder._get_okx_client()
    assert client.cancel_listing("confirmed-order") is True
    assert raw.cancel_calls == [(('confirmed-order',), {})]


def test_legacy_sell_relist_control_flow_continues_on_cancel_exception():
    source = inspect.getsource(CounterBidder._run_sell_phase)
    cancel_pos = source.index("client.cancel_listing")
    discard_pos = source.index("our_listed_token_ids.discard", cancel_pos)
    between = source[cancel_pos:discard_pos]

    assert "except Exception as exc:" in between
    assert "continue" in between


def test_failure_message_truncates_long_order_id():
    raw = _FakeClient(cancel_result=False)
    proxy = _SellCancelConfirmationProxy(raw)
    long_id = "order-abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(SellCancelNotConfirmed) as caught:
        proxy.cancel_listing(long_id)

    message = str(caught.value)
    assert long_id[:14] in message
    assert long_id not in message
