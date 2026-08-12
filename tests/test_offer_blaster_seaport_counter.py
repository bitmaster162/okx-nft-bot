from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.sniper.offer_blaster import BlasterResult, OfferBlaster


class _FakeMarketClient:
    def __init__(self, *, settings):
        self.settings = settings


class _FakeOKXAPIClient:
    payloads = []

    def __init__(self, *, settings, market_client=None):
        self.settings = settings
        self.market_client = market_client

    def submit_offer(self, payload):
        self.__class__.payloads.append(payload)
        return {"offer_id": f"offer-{len(self.__class__.payloads)}"}


def test_eth_offer_blast_reuses_same_seaport_counter_for_multiple_orders(monkeypatch):
    import okx_nft_bot.clients.okx as okx_module
    import okx_nft_bot.counterbid.okx_api as okx_api_module

    monkeypatch.setattr(okx_module, "OKXMarketplaceClient", _FakeMarketClient)
    monkeypatch.setattr(okx_api_module, "OKXAPIClient", _FakeOKXAPIClient)
    _FakeOKXAPIClient.payloads.clear()

    settings = SimpleNamespace(
        buyer_wallet_address="0x" + "1" * 40,
        buyer_wallet_private_key="0x" + "2" * 64,
    )

    blaster = OfferBlaster.__new__(OfferBlaster)
    blaster.max_per_collection = 2
    blaster.delay_seconds = 0.0
    blaster.duration_hours = 24
    blaster.undercut_bps = 100
    blaster._get_settings = lambda: settings
    blaster._get_seaport_counter = lambda *_args, **_kwargs: 7
    blaster._fetch_collection_nfts_paginated = lambda *_args, **_kwargs: [
        {"tokenId": "1", "ownerAddress": "0x" + "3" * 40},
        {"tokenId": "2", "ownerAddress": "0x" + "4" * 40},
    ]
    blaster._fetch_existing_offers = lambda *_args, **_kwargs: {}

    built_counters = []

    def build_offer(**kwargs):
        built_counters.append(kwargs["counter"])
        return {"counter": str(kwargs["counter"]), "token_id": kwargs["token_id"]}

    blaster._build_eth_offer = build_offer
    blaster._sign_eth_order = lambda *_args, **_kwargs: "0xsig"

    result = BlasterResult(
        collection_address="0x" + "5" * 40,
        collection_name="counter-test",
        chain="eth",
        offers_placed=0,
        offers_failed=0,
        offers_skipped=0,
        price_used=0.001,
        currency="WETH",
        dry_run=False,
    )

    blaster._blast_eth(
        result,
        result.collection_address,
        0.001,
        False,
    )

    assert result.offers_placed == 2
    assert result.offers_failed == 0
    assert built_counters == [7, 7]
    assert [payload["counter"] for payload in _FakeOKXAPIClient.payloads] == [7, 7]
    assert [payload["parameters"]["counter"] for payload in _FakeOKXAPIClient.payloads] == ["7", "7"]
