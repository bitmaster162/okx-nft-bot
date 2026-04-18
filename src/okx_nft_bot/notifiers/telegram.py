from __future__ import annotations

from urllib import parse

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier
from okx_nft_bot.notifiers.formatters import format_text


class TelegramNotifier(Notifier):
    channel = "telegram"

    def __init__(self, *, bot_token: str, chat_id: str, transport: StdlibHttpTransport) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.transport = transport

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        payload = {
            "chat_id": self.chat_id,
            "text": format_text(alert),
        }
        body = parse.urlencode(payload)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        self.transport.request_json(
            method="POST",
            url=url,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=body,
        )
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)
