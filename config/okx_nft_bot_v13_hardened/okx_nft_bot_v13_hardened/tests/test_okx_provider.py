from pathlib import Path

from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.providers.okx_marketplace import OKXMarketplaceTradesProvider


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def request_json(self, *, method: str, url: str, headers: dict[str, str], body: str = "") -> dict:
        self.calls.append((method, url, headers, body))
        if "markets/trades" in url:
            return {
                "code": 0,
                "data": {
                    "cursor": "next-cursor",
                    "data": [
                        {
                            "amount": 1,
                            "chain": "Ethereum",
                            "collectionAddress": "0xabc",
                            "currencyAddress": "0x0000000000000000000000000000000000000000",
                            "from": "0xseller",
                            "platform": "OKX",
                            "price": 9.71,
                            "timestamp": 1720113467,
                            "to": "0xbuyer",
                            "tokenId": "4382",
                            "txHash": "0xtrade",
                        }
                    ],
                },
                "msg": "",
            }
        if "collection/detail" in url:
            return {
                "code": 0,
                "data": {
                    "name": "Test Collection",
                    "floorPrice": 8.5,
                    "volume24h": 120.0,
                },
                "msg": "",
            }
        raise AssertionError(f"Unexpected URL: {url}")


def build_settings() -> Settings:
    return Settings(
        app_env="test",
        db_path=Path("test.sqlite3"),
        okx_api_base="https://web3.okx.com",
        okx_api_key="key",
        okx_api_secret="secret",
        okx_api_passphrase="pass",
        okx_chain="eth",
        okx_collection_address="0xabc",
        okx_collection_slug="test-collection",
        okx_platform=None,
        okx_page_limit=20,
        okx_request_timeout=20,
        okx_max_retries=3,
        okx_rate_limit_per_sec=5.0,
        okx_enable_details=False,
        okx_max_pages_per_run=5,
        okx_cursor_namespace="test",
        collection_allowlist=(),
        min_price=None,
        min_volume=None,
        rules_path=Path("rule_packs.json"),
        telegram_bot_token=None,
        telegram_chat_id=None,
        webhook_url=None,
        notification_mode="passed_only",
    )


def test_trades_provider_maps_okx_response_into_raw_events() -> None:
    settings = build_settings()
    transport = FakeTransport()
    client = OKXMarketplaceClient(settings=settings, transport=transport)
    provider = OKXMarketplaceTradesProvider(client=client, settings=settings)

    page = provider.fetch_page(cursor=None)

    events = page["events"]
    assert len(events) == 1
    assert page["next_cursor"] == "next-cursor"
    payload = events[0].payload
    assert payload["market"] == "okx"
    assert payload["event_type"] == "sale"
    assert payload["collection"] == "Test Collection"
    assert payload["token_id"] == "4382"
    assert payload["contract_address"] == "0xabc"
    assert payload["price"] == 9.71
    assert payload["maker"] == "0xseller"
    assert payload["taker"] == "0xbuyer"
    assert payload["tx_hash"] == "0xtrade"
    assert payload["floor_price"] == 8.5
    assert payload["volume_24h"] == 120.0
