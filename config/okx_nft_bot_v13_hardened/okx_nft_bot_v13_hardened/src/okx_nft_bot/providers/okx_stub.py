from __future__ import annotations

from datetime import datetime, timezone

from okx_nft_bot.models import RawEvent
from okx_nft_bot.providers.base import Provider


class OKXStubProvider(Provider):
    def fetch_events(self) -> list[RawEvent]:
        now = datetime.now(timezone.utc).isoformat()
        payloads = [
            {
                "source": "okx_stub",
                "payload": {
                    "event_id": "okx-demo-sale-1",
                    "market": "okx",
                    "event_type": "sale",
                    "collection": "Demo Apes",
                    "token_id": "101",
                    "contract_address": "0xdemo",
                    "price": 2.5,
                    "currency": "ETH",
                    "quantity": 1,
                    "maker": "0xseller",
                    "taker": "0xbuyer",
                    "tx_hash": "0xtx1",
                    "event_time": now,
                    "volume_24h": 320.0,
                    "floor_price": 1.9,
                },
            },
            {
                "source": "okx_stub",
                "payload": {
                    "event_id": "okx-demo-listing-2",
                    "market": "okx",
                    "event_type": "listing",
                    "collection": "Demo Cats",
                    "token_id": "22",
                    "contract_address": "0xcat",
                    "price": 0.8,
                    "currency": "ETH",
                    "quantity": 1,
                    "maker": "0xmaker",
                    "event_time": now,
                    "volume_24h": 55.0,
                    "floor_price": 0.75,
                },
            },
        ]
        return [RawEvent.model_validate(item) for item in payloads]
