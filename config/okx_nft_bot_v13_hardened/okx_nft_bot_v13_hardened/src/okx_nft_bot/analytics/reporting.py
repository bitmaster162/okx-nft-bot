from __future__ import annotations

import json
from dataclasses import asdict
from urllib import parse

from okx_nft_bot.analytics.cross_market import CollectionScore, SpreadOpportunity
from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings


def format_spreads_text(items: list[SpreadOpportunity]) -> str:
    if not items:
        return 'No spread opportunities detected'
    lines = ['Cross-market spreads:']
    for item in items:
        currency = item.reference_currency or ''
        observed = item.observed_at.isoformat() if item.observed_at else 'n/a'
        lines.append(
            f"- {item.collection_name}: buy {item.buy_market} @ {item.buy_price:.4f} {currency} | "
            f"sell {item.sell_market} @ {item.sell_price:.4f} {currency} | "
            f"spread={item.spread_pct:.2f}% ({item.spread_abs:.4f}) | observed={observed}"
        )
    return '\n'.join(lines)


def format_rankings_text(items: list[CollectionScore]) -> str:
    if not items:
        return 'No collection rankings available'
    lines = ['Collection ranking:']
    for idx, item in enumerate(items, start=1):
        observed = item.latest_event_time.isoformat() if item.latest_event_time else 'n/a'
        signals = ', '.join(item.signals) if item.signals else 'none'
        lines.append(
            f"{idx}. {item.collection_name} | score={item.score:.2f} | markets={item.market_count} | "
            f"events={item.event_count} | spread={item.best_spread_pct:.2f}% | latest={observed} | signals={signals}"
        )
    return '\n'.join(lines)


def build_analytics_payload(spreads: list[SpreadOpportunity], rankings: list[CollectionScore]) -> dict[str, object]:
    return {
        'spread_count': len(spreads),
        'ranking_count': len(rankings),
        'spreads': [asdict(item) | {'observed_at': item.observed_at.isoformat() if item.observed_at else None} for item in spreads],
        'rankings': [asdict(item) | {'latest_event_time': item.latest_event_time.isoformat() if item.latest_event_time else None, 'signals': list(item.signals)} for item in rankings],
        'spread_text': format_spreads_text(spreads),
        'ranking_text': format_rankings_text(rankings),
    }


def send_analytics_report(settings: Settings, *, spreads: list[SpreadOpportunity], rankings: list[CollectionScore]) -> dict[str, object]:
    payload = build_analytics_payload(spreads, rankings)
    sent = {'telegram': False, 'webhook': False}
    text = payload['spread_text'] + '\n\n' + payload['ranking_text']
    transport = StdlibHttpTransport(
        timeout=max(settings.okx_request_timeout, settings.opensea_request_timeout),
        max_retries=max(settings.okx_max_retries, settings.opensea_max_retries),
        rate_limit_per_sec=min(settings.okx_rate_limit_per_sec, settings.opensea_rate_limit_per_sec),
    )
    if settings.telegram_bot_token and settings.telegram_chat_id:
        body = parse.urlencode({'chat_id': settings.telegram_chat_id, 'text': text})
        transport.request_json(
            method='POST',
            url=f'https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage',
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            body=body,
        )
        sent['telegram'] = True
    if settings.webhook_url:
        transport.request_json(
            method='POST',
            url=settings.webhook_url,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            body=json.dumps({'kind': 'analytics_report', **payload}, ensure_ascii=False),
        )
        sent['webhook'] = True
    return {'sent': sent, **payload}
