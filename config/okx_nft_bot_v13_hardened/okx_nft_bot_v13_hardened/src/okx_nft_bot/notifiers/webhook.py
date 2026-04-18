from __future__ import annotations

from okx_nft_bot.clients.http import StdlibHttpTransport
from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier
from okx_nft_bot.notifiers.formatters import format_webhook_json


class WebhookNotifier(Notifier):
    channel = "webhook"

    def __init__(self, *, webhook_url: str, transport: StdlibHttpTransport) -> None:
        self.webhook_url = webhook_url
        self.transport = transport

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        try:
            self.transport.request_json(
                method="POST",
                url=self.webhook_url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=format_webhook_json(alert),
            )
            return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=True)
        except Exception:
            return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=False)
