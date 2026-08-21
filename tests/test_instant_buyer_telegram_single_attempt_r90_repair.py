from __future__ import annotations

import sys
import types
import urllib.request

from okx_nft_bot.sniper.buyer import BuyAttempt, OKXInstantBuyer


def _buyer() -> OKXInstantBuyer:
    buyer = OKXInstantBuyer.__new__(OKXInstantBuyer)
    buyer.tg_token = "test-token"
    buyer.tg_chat = "test-chat"
    return buyer


def _attempt() -> BuyAttempt:
    return BuyAttempt(
        collection_address="0xcollection",
        collection_name="R90",
        token_id="1",
        chain="eth",
        listing_price=0.01,
        currency="ETH",
        max_buy_price=0.02,
        success=True,
        tx_hash="0xtx",
        dry_run=False,
    )


def test_unknown_telegram_outcome_has_only_one_external_send_attempt(monkeypatch) -> None:
    calls = {"legacy": 0, "fallback": 0, "transport": 0}

    fake_sales_stream = types.ModuleType("okx_nft_bot.sales_stream")

    class LegacyHttp:
        @staticmethod
        def post(*args, **kwargs):
            calls["legacy"] += 1
            raise RuntimeError("response lost after provider may have accepted send")

    fake_sales_stream.http = LegacyHttp()
    monkeypatch.setitem(sys.modules, "okx_nft_bot.sales_stream", fake_sales_stream)

    def fake_urlopen(*args, **kwargs):
        calls["fallback"] += 1
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    from okx_nft_bot.clients import http as http_module

    def fake_request_json(self, **kwargs):
        calls["transport"] += 1
        assert self.max_retries == 1
        raise RuntimeError("response lost after provider may have accepted send")

    monkeypatch.setattr(http_module.StdlibHttpTransport, "request_json", fake_request_json)

    _buyer()._alert_buy_attempt(_attempt())

    assert sum(calls.values()) == 1, calls


def test_buyer_telegram_uses_single_attempt_transport_and_checks_provider_ack(monkeypatch, caplog) -> None:
    constructed: dict[str, object] = {}
    request_calls = 0

    fake_sales_stream = types.ModuleType("okx_nft_bot.sales_stream")

    class LegacyHttp:
        @staticmethod
        def post(*args, **kwargs):
            raise AssertionError("legacy direct Telegram POST must not be used")

    fake_sales_stream.http = LegacyHttp()
    monkeypatch.setitem(sys.modules, "okx_nft_bot.sales_stream", fake_sales_stream)

    def no_fallback(*args, **kwargs):
        raise AssertionError("urllib fallback resend must not be used")

    monkeypatch.setattr(urllib.request, "urlopen", no_fallback)

    from okx_nft_bot.clients import http as http_module

    class FakeTransport:
        def __init__(self, *, timeout: int, max_retries: int, rate_limit_per_sec: float) -> None:
            constructed.update(
                timeout=timeout,
                max_retries=max_retries,
                rate_limit_per_sec=rate_limit_per_sec,
            )

        def request_json(self, **kwargs):
            nonlocal request_calls
            request_calls += 1
            return {"ok": False}

    monkeypatch.setattr(http_module, "StdlibHttpTransport", FakeTransport)

    _buyer()._alert_buy_attempt(_attempt())

    assert constructed["max_retries"] == 1
    assert request_calls == 1
    assert "telegram_rejected" in caplog.text
