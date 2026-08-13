from __future__ import annotations

import inspect

import pytest

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.sniper.batch_cancel_effect_safety import (
    _BatchCancelEffectProxy,
    install_batch_cancel_effect_safety,
)
from okx_nft_bot.sniper.counter_bidder import CounterBidder


class _Owner:
    pass


class _FakeClient:
    def __init__(
        self,
        *,
        batch_result=True,
        batch_exc: Exception | None = None,
        cancel_result=True,
    ):
        self.batch_result = batch_result
        self.batch_exc = batch_exc
        self.cancel_result = cancel_result
        self.batch_calls: list[tuple[tuple, dict]] = []
        self.cancel_calls: list[tuple[tuple, dict]] = []
        self.create_calls: list[tuple[tuple, dict]] = []
        self.read_calls: list[tuple[str, tuple, dict]] = []

    def cancel_all_via_counter(self, *args, **kwargs):
        self.batch_calls.append((args, kwargs))
        if self.batch_exc is not None:
            raise self.batch_exc
        return self.batch_result

    def cancel_offer(self, *args, **kwargs):
        self.cancel_calls.append((args, kwargs))
        return self.cancel_result

    def create_listing(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))
        return {"status": "delegated"}

    def cancel_listing(self, *args, **kwargs):
        return True

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
def test_non_literal_true_batch_result_marks_uncertain_and_suppresses_fallback(result):
    owner = _Owner()
    raw = _FakeClient(batch_result=result)
    proxy = _BatchCancelEffectProxy(raw, owner)

    assert proxy.cancel_all_via_counter(chain="bsc") is False
    assert raw.batch_calls == [((), {"chain": "bsc"})]

    # The dangerous second effect is blocked rather than delegated.
    assert proxy.cancel_offer("old-order", chain="bsc") is False
    assert raw.cancel_calls == []


def test_literal_true_batch_confirmation_keeps_normal_cancel_delegation():
    owner = _Owner()
    raw = _FakeClient(batch_result=True, cancel_result=True)
    proxy = _BatchCancelEffectProxy(raw, owner)

    assert proxy.cancel_all_via_counter(chain="eth") is True
    assert proxy.cancel_offer("later-order", chain="eth") is True
    assert raw.batch_calls == [((), {"chain": "eth"})]
    assert raw.cancel_calls == [(("later-order",), {"chain": "eth"})]


def test_batch_exception_is_not_retried_and_blocks_second_effect_after_catch():
    owner = _Owner()
    ambiguous = RuntimeError("receipt timeout after broadcast")
    raw = _FakeClient(batch_exc=ambiguous)
    proxy = _BatchCancelEffectProxy(raw, owner)

    with pytest.raises(RuntimeError, match="receipt timeout after broadcast") as caught:
        proxy.cancel_all_via_counter(chain="bsc")

    assert caught.value is ambiguous
    assert raw.batch_calls == [((), {"chain": "bsc"})]

    # Simulate the legacy caller catching the batch exception and entering its
    # per-order fallback loop.  R60 turns that fallback into a no-effect False.
    assert proxy.cancel_offer("old-order", chain="bsc") is False
    assert raw.cancel_calls == []


def test_non_cancel_effect_method_delegates_unchanged():
    owner = _Owner()
    raw = _FakeClient()
    proxy = _BatchCancelEffectProxy(raw, owner)

    result = proxy.create_listing(token_id="0", price_raw="123")

    assert result == {"status": "delegated"}
    assert raw.create_calls == [((), {"token_id": "0", "price_raw": "123"})]
    assert raw.batch_calls == []
    assert raw.cancel_calls == []


def test_installer_is_idempotent_and_preserves_none_client():
    class DummyBidder:
        def __init__(self, client):
            self.client = client

        def _get_okx_client(self):
            return self.client

        def _cancel_existing_offer(self, *args, **kwargs):
            return True

    install_batch_cancel_effect_safety(DummyBidder)
    first_get = DummyBidder._get_okx_client
    first_cancel = DummyBidder._cancel_existing_offer
    install_batch_cancel_effect_safety(DummyBidder)

    assert DummyBidder._get_okx_client is first_get
    assert DummyBidder._cancel_existing_offer is first_cancel
    assert getattr(first_get, "_r60_batch_cancel_effect_proxy", False) is True
    assert getattr(first_cancel, "_r60_batch_cancel_effect_guard", False) is True
    assert DummyBidder(None)._get_okx_client() is None


def test_operation_scope_resets_suppression_after_unconfirmed_batch():
    class DummyBidder:
        def __init__(self, client):
            self.client = client

        def _get_okx_client(self):
            return self.client

        def _cancel_existing_offer(self):
            client = self._get_okx_client()
            batch_ok = client.cancel_all_via_counter(chain="bsc")
            if batch_ok:
                return True
            return client.cancel_offer("fallback-order", chain="bsc")

    raw = _FakeClient(batch_result=False, cancel_result=True)
    install_batch_cancel_effect_safety(DummyBidder)
    bidder = DummyBidder(raw)

    # During the operation the fallback is suppressed.
    assert bidder._cancel_existing_offer() is False
    assert raw.batch_calls == [((), {"chain": "bsc"})]
    assert raw.cancel_calls == []

    # The finally block clears the owner-scoped flag, so an unrelated later
    # per-order cancel delegates normally.
    assert bidder._get_okx_client().cancel_offer("later", chain="bsc") is True
    assert raw.cancel_calls == [(("later",), {"chain": "bsc"})]


def _make_counter_bidder(raw: _FakeClient, offers_sequence: list[list[dict]]):
    bidder = CounterBidder.__new__(CounterBidder)
    bidder.dry_run = False
    bidder._okx_client = raw
    bidder._local_placed_offers = {}
    sequence = iter(offers_sequence)
    bidder._find_all_our_offers = lambda addr, chain: next(sequence)
    return bidder


def _offers(count: int) -> list[dict]:
    return [
        {
            "order_id": f"order-{idx}",
            "price": 0.1 + idx * 0.01,
            "currency": "WBNB",
            "order_params": {"counter": 1},
        }
        for idx in range(count)
    ]


def test_package_legacy_batch_false_cannot_cross_into_per_order_effect(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.sniper.counter_bidder.time.sleep", lambda *_: None)
    raw = _FakeClient(batch_result=False, cancel_result=True)
    bidder = _make_counter_bidder(raw, [_offers(2)])

    assert bidder._cancel_existing_offer("0xabc", "bsc", "demo") is False
    assert len(raw.batch_calls) == 1
    assert raw.cancel_calls == []
    assert getattr(bidder, "_r60_batch_cancel_unconfirmed", False) is False


def test_package_legacy_batch_exception_cannot_cross_into_per_order_effect(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.sniper.counter_bidder.time.sleep", lambda *_: None)
    raw = _FakeClient(batch_exc=RuntimeError("receipt timeout"), cancel_result=True)
    bidder = _make_counter_bidder(raw, [_offers(3)])

    # Legacy catches the batch exception, but R60 suppresses every attempted
    # fallback cancel and leaves the whole operation failed closed.
    assert bidder._cancel_existing_offer("0xabc", "eth", "demo") is False
    assert len(raw.batch_calls) == 1
    assert raw.cancel_calls == []
    assert getattr(bidder, "_r60_batch_cancel_unconfirmed", False) is False


def test_package_confirmed_batch_success_still_uses_existing_success_path(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.sniper.counter_bidder.time.sleep", lambda *_: None)
    raw = _FakeClient(batch_result=True, cancel_result=True)
    bidder = _make_counter_bidder(raw, [_offers(2), []])

    assert bidder._cancel_existing_offer("0xabc", "bsc", "demo") is True
    assert len(raw.batch_calls) == 1
    assert raw.cancel_calls == []
    assert getattr(bidder, "_r60_batch_cancel_unconfirmed", False) is False


def test_package_single_offer_path_is_not_suppressed(monkeypatch):
    monkeypatch.setattr("okx_nft_bot.sniper.counter_bidder.time.sleep", lambda *_: None)
    raw = _FakeClient(batch_result=False, cancel_result=True)
    bidder = _make_counter_bidder(raw, [_offers(1), []])

    assert bidder._cancel_existing_offer("0xabc", "bsc", "demo") is True
    assert raw.batch_calls == []
    assert raw.cancel_calls == [(("order-0",), {"chain": "bsc", "order_params": {"counter": 1}})]
    assert getattr(bidder, "_r60_batch_cancel_unconfirmed", False) is False


def test_package_r58_r59_r60_client_layers_still_compose():
    raw = _FakeClient(batch_result=True)
    bidder = CounterBidder.__new__(CounterBidder)
    bidder._okx_client = raw

    client = bidder._get_okx_client()

    inventory = client.get_wallet_nfts(chain="bsc", wallet_address="0xabc")
    listings = client.get_listings(chain="bsc", collection_address="0xdef")
    assert inventory["data"]["data"][0]["tokenId"] == "0"
    assert listings["data"]["data"][0]["tokenId"] == "0"

    accessor = CounterBidder._get_okx_client
    assert getattr(accessor, "_r58_sell_token_zero_scope", False) is True
    assert getattr(accessor, "_r59_sell_cancel_confirmation", False) is True
    assert getattr(accessor, "_r60_batch_cancel_effect_proxy", False) is True
    assert getattr(CounterBidder._cancel_existing_offer, "_r60_batch_cancel_effect_guard", False) is True


def test_legacy_source_contains_batch_to_per_order_fallback_that_r60_guards():
    legacy = inspect.unwrap(CounterBidder._cancel_existing_offer)
    source = inspect.getsource(legacy)

    batch_pos = source.index("cancel_all_via_counter")
    fallback_pos = source.index("client.cancel_offer", batch_pos)
    between = source[batch_pos:fallback_pos]

    assert "if batch_ok:" in between
    assert "falling back to per-order" in between


def test_underlying_batch_implementation_has_broadcast_then_receipt_boundary():
    source = inspect.getsource(OKXAPIClient._bump_counter_onchain)
    send_pos = source.index("send_raw_transaction")
    receipt_pos = source.index("wait_for_transaction_receipt", send_pos)

    assert send_pos < receipt_pos
    assert "except Exception as exc:" in source[receipt_pos:]
    assert "return False" in source[receipt_pos:]
