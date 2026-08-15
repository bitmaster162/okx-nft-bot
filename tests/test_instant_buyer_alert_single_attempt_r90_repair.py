from __future__ import annotations

import logging

from okx_nft_bot.sniper.buyer import BuyAttempt, OKXInstantBuyer


def _buyer() -> OKXInstantBuyer:
    buyer = object.__new__(OKXInstantBuyer)
    buyer.tg_token = "test-token"
    buyer.tg_chat = "12345"
    return buyer


def _attempt() -> BuyAttempt:
    return BuyAttempt(
        collection_address="0xabc",
        collection_name="Regression Collection",
        token_id="7",
        chain="eth",
        listing_price=0.01,
        currency="ETH",
        max_buy_price=0.02,
        success=True,
        tx_hash="0x1234",
        latency_ms=10,
        dry_run=False,
    )


def test_instant_buyer_alert_unknown_outcome_is_single_attempt(monkeypatch):
    calls = {"transport": 0, "legacy_post": 0, "legacy_fallback": 0}

    def transport_unknown(self, **kwargs):
        calls["transport"] += 1
        raise TimeoutError("response lost after provider may have accepted send")

    def legacy_unknown(*args, **kwargs):
        calls["legacy_post"] += 1
        raise TimeoutError("response lost after provider may have accepted send")

    def legacy_fallback(*args, **kwargs):
        calls["legacy_fallback"] += 1
        raise AssertionError("unknown notification outcome must never be retried")

    from okx_nft_bot.clients.http import StdlibHttpTransport
    from okx_nft_bot.sales_stream import http as sales_http
    import urllib.request

    monkeypatch.setattr(StdlibHttpTransport, "request_json", transport_unknown)
    monkeypatch.setattr(sales_http, "post", legacy_unknown)
    monkeypatch.setattr(urllib.request, "urlopen", legacy_fallback)

    _buyer()._alert_buy_attempt(_attempt())

    assert sum(calls.values()) == 1, calls


def test_instant_buyer_alert_requires_telegram_provider_ack(monkeypatch, caplog):
    calls = {"transport": 0}

    def provider_rejects(self, **kwargs):
        calls["transport"] += 1
        return {"ok": False, "description": "chat not found"}

    def legacy_path(*args, **kwargs):
        raise AssertionError("legacy Telegram sender must not be used")

    from okx_nft_bot.clients.http import StdlibHttpTransport
    from okx_nft_bot.sales_stream import http as sales_http
    import urllib.request

    monkeypatch.setattr(StdlibHttpTransport, "request_json", provider_rejects)
    monkeypatch.setattr(sales_http, "post", legacy_path)
    monkeypatch.setattr(urllib.request, "urlopen", legacy_path)

    caplog.set_level(logging.WARNING, logger="sniper.buyer")
    _buyer()._alert_buy_attempt(_attempt())

    assert calls["transport"] == 1
    assert "Telegram notify rejected by provider" in caplog.text
