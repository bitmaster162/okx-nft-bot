from __future__ import annotations

import json

from okx_nft_bot.notifiers.base import AlertEnvelope

# Native token zero-addresses → human-readable symbol
_NATIVE_ADDRS = {
    '0x0000000000000000000000000000000000000000',
    '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
}
_WEI = 10 ** 18


def _build_nft_url(market: str, contract_address: str | None, token_id: str) -> str | None:
    """Build a direct link to the NFT page on the marketplace so the user can open and buy manually."""
    if not contract_address:
        return None
    if market == 'okx':
        # OKX NFT marketplace URL: /web3/nft/detail/<contract>/<token_id>
        return f'https://www.okx.com/web3/nft/detail/{contract_address}/{token_id}'
    if market == 'opensea':
        return f'https://opensea.io/assets/matic/{contract_address}/{token_id}'
    if market == 'magiceden':
        return f'https://magiceden.io/item-details/{contract_address}:{token_id}'
    return None


def _format_price(price: float | None, currency: str | None, market: str) -> str:
    if price is None:
        return 'n/a'
    # Detect wei: OKX returns raw wei for BNB/ETH prices
    # If price > 1_000_000 and currency is native address → convert
    symbol = 'BNB' if market in ('okx',) else 'ETH'
    if currency and currency.lower() in {a.lower() for a in _NATIVE_ADDRS}:
        symbol = 'BNB' if market == 'okx' else 'ETH'
    elif currency and len(currency) != 42:
        symbol = currency  # already a symbol like 'WETH'
    elif currency and currency.lower() not in {a.lower() for a in _NATIVE_ADDRS}:
        symbol = currency  # ERC-20 contract address, show as-is for now

    if price > 1_000_000:
        converted = price / _WEI
        return f'{converted:.4f} {symbol}'
    return f'{price:.4f} {symbol}'


def format_text(alert: AlertEnvelope) -> str:
    event = alert.event
    price_str = _format_price(event.price, event.currency, event.market)
    rules = ', '.join(alert.decision.matched_rules) if alert.decision.matched_rules else 'none'
    market_label = event.market.upper()
    _EVENT_EMOJI = {'sale': '🛒', 'listing': '🏷️', 'offer': '💎'}
    event_emoji = _EVENT_EMOJI.get(event.event_type, '📊')
    nft_url = _build_nft_url(event.market, event.contract_address, event.token_id)
    link_line = f"🔗 {nft_url}" if nft_url else '🔗 n/a'
    return (
        f"[{market_label} NFT BOT] {event_emoji} {event.event_type.upper()}\n"
        f"📦 {event.collection}\n"
        f"🔑 token: {event.token_id}\n"
        f"💰 price: {price_str}\n"
        f"🕐 {event.event_time.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"{link_line}\n"
        f"✅ rules: {rules}"
    )


def format_webhook_payload(alert: AlertEnvelope) -> dict[str, object]:
    return {
        "event": alert.event.model_dump(mode="json"),
        "decision": alert.decision.model_dump(mode="json"),
        "text": format_text(alert),
    }


def format_webhook_json(alert: AlertEnvelope) -> str:
    return json.dumps(format_webhook_payload(alert), ensure_ascii=False)
