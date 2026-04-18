from __future__ import annotations

from abc import ABC, abstractmethod

from okx_nft_bot.models import RawEvent


class Provider(ABC):
    @abstractmethod
    def fetch_events(self) -> list[RawEvent]:
        """Return raw provider payloads."""
