from __future__ import annotations

from dataclasses import dataclass

from okx_nft_bot.models import DeliveryResult, FilterDecision, NFTEvent


@dataclass(slots=True)
class AlertEnvelope:
    event: NFTEvent
    decision: FilterDecision


class Notifier:
    channel: str = "unknown"

    def send(self, alert: AlertEnvelope) -> DeliveryResult:
        raise NotImplementedError
