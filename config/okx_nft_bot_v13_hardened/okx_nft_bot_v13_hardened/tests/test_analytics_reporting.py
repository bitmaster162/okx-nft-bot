from pathlib import Path

from okx_nft_bot.analytics.cross_market import CollectionScore, SpreadOpportunity
from okx_nft_bot.analytics.reporting import format_rankings_text, format_spreads_text, send_analytics_report
from okx_nft_bot.config import load_settings


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def request_json(self, *, method, url, headers, body=''):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        return {'ok': True}


def test_reporting_text_formats() -> None:
    spreads = [SpreadOpportunity(collection_key='0xabc', collection_name='Alpha', buy_market='okx', sell_market='opensea', buy_price=1.0, sell_price=1.25, spread_abs=0.25, spread_pct=25.0, observed_at=None, markets_seen=2, reference_currency='ETH')]
    rankings = [CollectionScore(collection_key='0xabc', collection_name='Alpha', score=88.5, market_count=2, event_count=10, listing_count=5, sale_count=5, volume_24h_total=100.0, best_spread_pct=25.0, latest_event_time=None, signals=('spread=25.00%',))]
    assert 'Cross-market spreads' in format_spreads_text(spreads)
    assert 'Collection ranking' in format_rankings_text(rankings)


def test_send_analytics_report_hits_telegram_and_webhook(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    monkeypatch.setenv('WEBHOOK_URL', 'https://example.test/hook')
    settings = load_settings()

    spreads = [SpreadOpportunity(collection_key='0xabc', collection_name='Alpha', buy_market='okx', sell_market='opensea', buy_price=1.0, sell_price=1.25, spread_abs=0.25, spread_pct=25.0, observed_at=None, markets_seen=2, reference_currency='ETH')]
    rankings = [CollectionScore(collection_key='0xabc', collection_name='Alpha', score=88.5, market_count=2, event_count=10, listing_count=5, sale_count=5, volume_24h_total=100.0, best_spread_pct=25.0, latest_event_time=None, signals=('spread=25.00%',))]

    fake_transport = FakeTransport()
    monkeypatch.setattr('okx_nft_bot.analytics.reporting.StdlibHttpTransport', lambda **kwargs: fake_transport)
    payload = send_analytics_report(settings, spreads=spreads, rankings=rankings)
    assert payload['sent']['telegram'] is True
    assert payload['sent']['webhook'] is True
    assert len(fake_transport.calls) == 2
