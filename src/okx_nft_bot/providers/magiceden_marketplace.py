"""
DEPRECATION WARNING (March 2025):
MagicEden shut down all EVM support (Ethereum, BSC, Polygon) in March 2025.
These providers are non-functional for EVM chains.

This module is kept for historical reference and potential future Solana support.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.magiceden import MagicEdenClient
from okx_nft_bot.config import Settings
from okx_nft_bot.currency import canonical_currency
from okx_nft_bot.models import RawEvent
from okx_nft_bot.providers.base import Provider


class MagicEdenTradesProvider(Provider):
    """Fetches sales from Magic Eden EVM (Reservoir activity API).

    DEPRECATED: MagicEden shut down EVM support in March 2025.
    This provider is non-functional for EVM chains. May be re-enabled for Solana.
    """

    def __init__(self, client: MagicEdenClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        return self.fetch_page(cursor=None)['events']

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        chain = self.settings.magiceden_chain
        collection = self.settings.magiceden_collection_address or self.settings.magiceden_collection_slug or ''
        if not collection:
            raise RuntimeError('MAGICEDEN_COLLECTION_ADDRESS or MAGICEDEN_COLLECTION_SLUG is required')

        payload = self.client.get_collection_activity(
            chain=chain,
            collection=collection,
            types=['sale'],
            continuation=cursor,
            limit=self.settings.magiceden_page_limit,
        )

        activities = _extract_list(payload, 'activities')
        next_cursor = payload.get('continuation') or None
        collection_name = self.settings.magiceden_collection_slug or collection

        events: list[RawEvent] = []
        for item in activities:
            if item.get('type') != 'sale':
                continue
            token = item.get('token') if isinstance(item.get('token'), dict) else {}
            token_id = str(token.get('tokenId') or item.get('token', {}).get('tokenId') or 'unknown')
            contract_address = item.get('contract') or token.get('contract')
            price_block = item.get('price') if isinstance(item.get('price'), dict) else {}
            amount_block = price_block.get('amount') if isinstance(price_block.get('amount'), dict) else {}
            currency_block = price_block.get('currency') if isinstance(price_block.get('currency'), dict) else {}
            payload_item = {
                'market': 'magiceden',
                'event_type': 'sale',
                'event_id': f'me_sale:{item.get("txHash") or item.get("orderId") or token_id}:{item.get("timestamp", 0)}',
                'collection': str(token.get('collection', {}).get('name') if isinstance(token.get('collection'), dict) else collection_name),
                'token_id': token_id,
                'contract_address': contract_address,
                'price': _to_float(amount_block.get('native') or amount_block.get('decimal') or price_block.get('amount')),
                'currency': canonical_currency(str(currency_block.get('symbol') or 'ETH')),
                'quantity': 1,
                'maker': item.get('fromAddress'),
                'taker': item.get('toAddress'),
                'tx_hash': item.get('txHash'),
                'event_time': _to_iso(item.get('timestamp')),
                'floor_price': None,
                'volume_24h': None,
            }
            events.append(RawEvent(source='magiceden_activity', payload=payload_item))

        return {'events': events, 'next_cursor': next_cursor if next_cursor else None}


class MagicEdenListingsProvider(Provider):
    """Fetches active listings from Magic Eden EVM (Reservoir asks API).

    DEPRECATED: MagicEden shut down EVM support in March 2025.
    This provider is non-functional for EVM chains. May be re-enabled for Solana.
    """

    def __init__(self, client: MagicEdenClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        return self.fetch_page(cursor=None)['events']

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        chain = self.settings.magiceden_chain
        collection = self.settings.magiceden_collection_address or self.settings.magiceden_collection_slug or ''
        if not collection:
            raise RuntimeError('MAGICEDEN_COLLECTION_ADDRESS or MAGICEDEN_COLLECTION_SLUG is required')

        payload = self.client.get_collection_listings(
            chain=chain,
            collection=collection,
            continuation=cursor,
            limit=self.settings.magiceden_page_limit,
        )

        orders = _extract_list(payload, 'orders')
        next_cursor = payload.get('continuation') or None
        collection_name = self.settings.magiceden_collection_slug or collection

        events: list[RawEvent] = []
        for item in orders:
            token_set_id = str(item.get('tokenSetId') or '')
            # tokenSetId is usually "token:CONTRACT:TOKEN_ID"
            parts = token_set_id.split(':')
            token_id = parts[-1] if len(parts) >= 3 else 'unknown'
            contract_address = item.get('contract') or (parts[1] if len(parts) >= 3 else None)
            price_block = item.get('price') if isinstance(item.get('price'), dict) else {}
            amount_block = price_block.get('amount') if isinstance(price_block.get('amount'), dict) else {}
            currency_block = price_block.get('currency') if isinstance(price_block.get('currency'), dict) else {}
            payload_item = {
                'market': 'magiceden',
                'event_type': 'listing',
                'event_id': f'me_listing:{item.get("id") or token_set_id}',
                'collection': collection_name,
                'token_id': token_id,
                'contract_address': contract_address,
                'price': _to_float(amount_block.get('native') or amount_block.get('decimal')),
                'currency': canonical_currency(str(currency_block.get('symbol') or 'ETH')),
                'quantity': int(item.get('quantityRemaining') or 1),
                'maker': item.get('maker'),
                'taker': item.get('taker'),
                'tx_hash': None,
                'event_time': _to_iso(item.get('validFrom') or item.get('createdAt')),
                'floor_price': None,
                'volume_24h': None,
            }
            events.append(RawEvent(source='magiceden_listings', payload=payload_item))

        return {'events': events, 'next_cursor': next_cursor if next_cursor else None}


def _extract_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = payload.get(key)
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    return []


def _to_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_iso(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    text = str(value)
    re