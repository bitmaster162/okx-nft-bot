from __future__ import annotations

from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier


class NullNotifier(Notifier):
    channel = "null"

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        return DeliveryResult(channel=self.channel, event_id=alert.event.event_id, delivered=False, detail="disabled")
