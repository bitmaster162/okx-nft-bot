from __future__ import annotations

from okx_nft_bot.models import DeliveryResult
from okx_nft_bot.notifiers.base import AlertEnvelope, Notifier


class FanoutNotifier(Notifier):
    channel = "fanout"

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        details: list[str] = []
        delivered = False
        for notifier in self.notifiers:
            result = notifier.send(alert)
            delivered = delivered or result.delivered
            details.append(f"{result.channel}:{'ok' if result.delivered else 'fail'}")
        return DeliveryResult(
            channel=self.channel,
            event_id=alert.event.event_id,
            delivered=delivered,
            detail=", ".join(details),
        )
