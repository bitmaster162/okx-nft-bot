from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.config import Settings
from okx_nft_bot.models import RawEvent
from okx_nft_bot.providers.base import Provider


class OpenSeaCollectionEventsProvider(Provider):
    def __init__(self, client: OpenSeaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        page = self.fetch_page(cursor=None)
        return page['events']

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        slug = self.settings.opensea_collection_slug
        if not slug:
            raise RuntimeError('OPENSEA_COLLECTION_SLUG is required for OpenSea provider')

        payload = self.client.get_collection_events(
            slug=slug,
            event_type='sale',
            cursor=cursor,
            limit=self.settings.opensea_page_limit,
        )
        stats_payload = self.client.get_collection_stats(slug=slug)
        collection_payload = self.client.get_collection(slug=slug)

        next_cursor = _extract_next_cursor(payload)
        stats = _extract_stats(stats_payload)
        collection = _extract_collection(collection_payload)
        records = _extract_records(payload)

        events: list[RawEvent] = []
        for item in records:
            nft = _extract_nft(item)
            token_id = str(nft.get('identifier') or nft.get('token_id') or item.get('item', {}).get('nft_id', 'unknown'))
            contract_address = nft.get('contract') or nft.get('contract_address')
            payment = item.get('payment') if isinstance(item.get('payment'), dict) else {}
            quantity = _to_int(item.get('quantity') or item.get('asset_quantity') or 1, default=1)
            payload_item = {
                'market': 'opensea',
                'event_type': 'sale',
                'event_id': _event_id('sale', item, token_id),
                'collection': str(collection.get('name') or slug),
                'token_id': token_id,
                'contract_address': contract_address,
                'price': _money_value(item.get('sale_price') or item.get('price') or payment.get('quantity')),
                'currency': payment.get('symbol') or payment.get('token') or payment.get('quantity_type'),
                'quantity': quantity,
                'maker': _extract_address(item, 'seller'),
                'taker': _extract_address(item, 'buyer'),
                'tx_hash': item.get('transaction') or item.get('transaction_hash'),
                'event_time': _to_iso(item.get('event_timestamp') or item.get('created_date') or item.get('listed_date')),
                'floor_price': _to_float(stats.get('floor_price')),
                'volume_24h': _to_float(stats.get('total', {}).get('one_day') if isinstance(stats.get('total'), dict) else stats.get('one_day_volume')),
                'event': item,
                'collection_stats': stats,
                'collection_details': collection,
                'next_cursor': next_cursor,
            }
            events.append(RawEvent(source='opensea_collection_events', payload=payload_item))
        return {'events': events, 'next_cursor': next_cursor}


class OpenSeaBestListingsProvider(Provider):
    def __init__(self, client: OpenSeaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_events(self) -> list[RawEvent]:
        page = self.fetch_page(cursor=None)
        return page['events']

    def fetch_page(self, cursor: str | None) -> dict[str, object]:
        slug = self.settings.opensea_collection_slug
        if not slug:
            raise RuntimeError('OPENSEA_COLLECTION_SLUG is required for OpenSea provider')
        payload = self.client.get_best_listings(slug=slug, cursor=cursor)
        stats_payload = self.client.get_collection_stats(slug=slug)
        collection_payload = self.client.get_collection(slug=slug)

        next_cursor = _extract_next_cursor(payload)
        stats = _extract_stats(stats_payload)
        collection = _extract_collection(collection_payload)
        records = _extract_listing_records(payload)

        events: list[RawEvent] = []
        for item in records:
            protocol_data = item.get('protocol_data') if isinstance(item.get('protocol_data'), dict) else {}
            parameters = protocol_data.get('parameters') if isinstance(protocol_data.get('parameters'), dict) else {}
            offer = parameters.get('offer') if isinstance(parameters.get('offer'), list) and parameters.get('offer') else []
            consideration = parameters.get('consideration') if isinstance(parameters.get('consideration'), list) and parameters.get('consideration') else []
            nft_side = offer[0] if offer else (consideration[0] if consideration else {})
            token_id = str(nft_side.get('identifierOrCriteria') or item.get('token_id') or 'unknown')
            contract_address = nft_side.get('token') or item.get('contract_address')
            payment_side = consideration[0] if consideration else {}
            payload_item = {
                'market': 'opensea',
                'event_type': 'listing',
                'event_id': _event_id('listing', item, token_id),
                'collection': str(collection.get('name') or slug),
                'token_id': token_id,
                'contract_address': contract_address,
                'price': _money_value(item.get('price', {}).get('current') if isinstance(item.get('price'), dict) else item.get('current_price') or payment_side.get('startAmount')),
                'currency': _currency_symbol(item) or payment_side.get('token'),
                'quantity': _to_int(nft_side.get('endAmount') or 1, default=1),
                'maker': parameters.get('offerer') or item.get('maker'),
                'taker': None,
                'tx_hash': item.get('order_hash') or item.get('orderHash'),
                'event_time': _to_iso(item.get('created_date') or item.get('listing_time') or item.get('created_at')),
                'floor_price': _to_float(stats.get('floor_price')),
                'volume_24h': _to_float(stats.get('total', {}).get('one_day') if isinstance(stats.get('total'), dict) else stats.get('one_day_volume')),
                'listing': item,
                'collection_stats': stats,
                'collection_details': collection,
                'next_cursor': next_cursor,
            }
            events.append(RawEvent(source='opensea_best_listings', payload=payload_item))
        return {'events': events, 'next_cursor': next_cursor}


def _extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get('asset_events')
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    events = payload.get('events')
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    return []


def _extract_listing_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    listings = payload.get('listings')
    if isinstance(listings, list):
        return [item for item in listings if isinstance(item, dict)]
    if isinstance(payload.get('orders'), list):
        return [item for item in payload['orders'] if isinstance(item, dict)]
    return []


def _extract_next_cursor(payload: dict[str, Any]) -> str | None:
    for key in ('next', 'next_cursor', 'cursor'):
        value = payload.get(key)
        if value not in {None, '', '0'}:
            return str(value)
    return None


def _extract_collection(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get('collection'), dict):
        return payload['collection']
    return payload if isinstance(payload, dict) else {}


def _extract_stats(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get('total'), dict) or 'floor_price' in payload:
        return payload
    if isinstance(payload.get('stats'), dict):
        return payload['stats']
    return {}


def _extract_nft(item: dict[str, Any]) -> dict[str, Any]:
    nft = item.get('nft')
    if isinstance(nft, dict):
        return nft
    item_block = item.get('item')
    if isinstance(item_block, dict):
        nft = item_block.get('nft')
        if isinstance(nft, dict):
            return nft
    return {}


def _extract_address(item: dict[str, Any], key: str) -> str | None:
    candidate = item.get(key)
    if isinstance(candidate, dict):
        for subkey in ('address', 'wallet_address', 'username'):
            value = candidate.get(subkey)
            if value:
                return str(value)
        return None
    if candidate:
        return str(candidate)
    return None


def _money_value(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ('current', 'amount', 'quantity', 'value'):
            if key in value:
                return _to_float(value.get(key))
        return None
    return _to_float(value)


def _currency_symbol(item: dict[str, Any]) -> str | None:
    payment = item.get('payment')
    if isinstance(payment, dict) and payment.get('symbol'):
        return str(payment['symbol'])
    price = item.get('price')
    if isinstance(price, dict) and isinstance(price.get('currency'), str):
        return str(price['currency'])
    protocol_data = item.get('protocol_data')
    if isinstance(protocol_data, dict):
        parameters = protocol_data.get('parameters')
        if isinstance(parameters, dict):
            consideration = parameters.get('consideration')
            if isinstance(consideration, list) and consideration and isinstance(consideration[0], dict):
                token = consideration[0].get('token')
                if token:
                    return str(token)
    return None


def _event_id(prefix: str, item: dict[str, Any], token_id: str) -> str:
    for key in ('event_id', 'order_hash', 'transaction', 'transaction_hash'):
        value = item.get(key)
        if value:
            return f'{prefix}:{value}:{token_id}'
    return f'{prefix}:{token_id}:{item.get("event_timestamp") or item.get("created_date") or "0"}'


def _to_iso(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    text = str(value)
    return text.replace('Z', '+00:00') if 'T' in text else datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
