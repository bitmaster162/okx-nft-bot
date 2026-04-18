from __future__ import annotations

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.config import Settings
from okx_nft_bot.notifiers.base import Notifier
from okx_nft_bot.notifiers.fanout import FanoutNotifier
from okx_nft_bot.notifiers.null import NullNotifier
from okx_nft_bot.notifiers.telegram import TelegramNotifier
from okx_nft_bot.notifiers.webhook import WebhookNotifier


def build_notifier(settings: Settings) -> Notifier:
    transport = StdlibHttpTransport(
        timeout=settings.okx_request_timeout,
        max_retries=settings.okx_max_retries,
        rate_limit_per_sec=settings.okx_rate_limit_per_sec,
    )
    notifiers: list[Notifier] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers.append(
            TelegramNotifier(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                transport=transport,
            )
        )
    if settings.webhook_url:
        notifiers.append(WebhookNotifier(webhook_url=settings.webhook_url, transport=transport))
    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return FanoutNotifier(notifiers)
