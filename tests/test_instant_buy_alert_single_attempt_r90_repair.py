from __future__ import annotations

import logging
import sys
import types
import urllib.request

from okx_nft_bot.sniper.buyer import BuyAttempt, OKXInstantBuyer


def _buyer() -> OKXInstantBuyer:
    buyer = object.__new__(OKXInstantBuyer)
    buyer.tg_token = "test-token"
    buyer.tg_chat = "test-chat"
    return buyer


def _attempt() -> BuyAttempt:
    return BuyAttempt(
        collection_address="0xcollection",
        collection_name="Test Collection",
        token_id="7",
        chain="bsc",
        listing_price=0.01,
        currency="BNB",
        max_buy_price=0.02,
        success=True,
        tx_hash="0xabc123",
        latency_ms=42,
        dry_run=False,
    )


def test_transport_exception_does_not_trigger_second_telegram_post(monkeypatch):
    calls = {"primary": 0, "fallback": 0}

    def primary_post(*args, **kwargs):
        calls["primary"] += 1
        raise RuntimeError("response lost after provider may have accepted request")

    fake_sales_stream = types.ModuleType("okx_nft_bot.sales_stream")
    fake_sales_stream.http = types.SimpleNamespace(post=primary_post)
    monkeypatch.setitem(sys.modules, "okx_nft_bot.sales_stream", fake_sales_stream)

    def fallback_urlopen(*args, **kwargs):
        calls["fallback"] += 1
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", fallback_urlopen)

    _buyer()._alert_buy_attempt(_attempt())

    assert calls["primary"] == 1
    assert calls["fallback"] == 0


def test_provider_rejection_is_not_silent(monkeypatch, caplog):
    class RejectedResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "description": "chat not found"}

    fake_sales_stream = types.ModuleType("okx_nft_bot.sales_stream")
    fake_sales_stream.http = types.SimpleNamespace(post=lambda *args, **kwargs: RejectedResponse())
    monkeypatch.setitem(sys.modules, "okx_nft_bot.sales_stream", fake_sales_stream)

    caplog.set_level(logging.WARNING, logger="sniper.buyer")
    _buyer()._alert_buy_attempt(_attempt())

    assert any("Telegram notify rejected" in record.getMessage() for record in caplog.records)
