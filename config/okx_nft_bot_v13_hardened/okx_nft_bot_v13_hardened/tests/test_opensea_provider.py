from pathlib import Path

from okx_nft_bot.config import Settings
from okx_nft_bot.clients.opensea import OpenSeaClient
from okx_nft_bot.providers.opensea_marketplace import OpenSeaBestListingsProvider, OpenSeaCollectionEventsProvider


class FakeTransport:
    def request_json(self, *, method, url, headers, body=''):
        if '/events/collection/' in url:
            return {
                'asset_events': [
                    {
                        'event_id': 'ev1',
                        'event_timestamp': '2026-03-07T00:00:00Z',
                        'sale_price': '1.5',
                        'payment': {'symbol': 'ETH'},
                        'seller': {'address': '0xseller'},
                        'buyer': {'address': '0xbuyer'},
                        'transaction': '0xtx',
                        'nft': {'identifier': '12', 'contract': '0xabc'},
                    }
                ],
                'next': 'cursor-2',
            }
        if '/stats' in url:
            return {'floor_price': 1.1, 'total': {'one_day': 12.3}}
        if '/collections/' in url and '/stats' not in url:
            return {'name': 'Pudgy Penguins'}
        if '/listings/collection/' in url:
            return {
                'listings': [
                    {
                        'order_hash': '0xorder',
                        'created_date': '2026-03-07T00:00:00Z',
                        'price': {'current': '2.25', 'currency': 'ETH'},
                        'protocol_data': {
                            'parameters': {
                                'offerer': '0xmaker',
                                'offer': [{'identifierOrCriteria': '55', 'token': '0xabc', 'endAmount': '1'}],
                                'consideration': [{'startAmount': '2250000000000000000', 'token': 'ETH'}],
                            }
                        },
                    }
                ],
                'next': 'cursor-3',
            }
        raise AssertionError(url)


def build_settings() -> Settings:
    return Settings(
        app_env='test',
        db_path=Path('test.sqlite3'),
        active_market='opensea',
        opensea_api_key='k',
        opensea_collection_slug='pudgy-penguins',
        rules_path=Path('rule_packs.json'),
    )


def test_opensea_events_provider_maps_sale() -> None:
    settings = build_settings()
    client = OpenSeaClient(settings=settings, transport=FakeTransport())
    provider = OpenSeaCollectionEventsProvider(client=client, settings=settings)
    page = provider.fetch_page(cursor=None)
    assert page['next_cursor'] == 'cursor-2'
    raw = page['events'][0]
    assert raw.payload['market'] == 'opensea'
    assert raw.payload['event_type'] == 'sale'
    assert raw.payload['collection'] == 'Pudgy Penguins'
    assert raw.payload['token_id'] == '12'


def test_opensea_listings_provider_maps_listing() -> None:
    settings = build_settings()
    client = OpenSeaClient(settings=settings, transport=FakeTransport())
    provider = OpenSeaBestListingsProvider(client=client, settings=settings)
    page = provider.fetch_page(cursor=None)
    raw = page['events'][0]
    assert raw.payload['event_type'] == 'listing'
    assert raw.payload['maker'] == '0xmaker'
    assert raw.payload['token_id'] == '55'
