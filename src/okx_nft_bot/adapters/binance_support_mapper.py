from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from okx_nft_bot.providers.binance_whitelist import BinanceWhitelistEntry


@dataclass(slots=True)
class BinanceSupportMetadata:
    binance_supported: bool
    binance_list_type: str | None
    support_confidence: str  # "confirmed", "likely", "unknown"
    support_source_url: str | None
    freshness: str | None  # ISO date of last verification
    confidence: str  # "reference_static"


_UNKNOWN = BinanceSupportMetadata(
    binance_supported=False,
    binance_list_type=None,
    support_confidence="unknown",
    support_source_url=None,
    freshness=None,
    confidence="reference_static",
)


class BinanceSupportMapper:
    """Maps collection identifiers to Binance support metadata.

    Lookup is case-insensitive by contract address or by name substring.
    """

    def __init__(self, entries: list[BinanceWhitelistEntry]) -> None:
        # Primary index: contract address (lowercase) → entry
        self._by_address: dict[str, BinanceWhitelistEntry] = {}
        # Secondary index: lowercased name → entry (for name-based lookup)
        self._by_name: dict[str, BinanceWhitelistEntry] = {}
        for entry in entries:
            addr = entry.contract_address_or_identifier.lower().strip()
            if addr:
                self._by_address[addr] = entry
            name_key = entry.collection_name.lower().strip()
            if name_key:
                self._by_name[name_key] = entry

    def lookup(self, *, address: str | None = None, name: str | None = None) -> BinanceSupportMetadata:
        """Look up support metadata by contract address (preferred) or collection name."""
        entry: BinanceWhitelistEntry | None = None

        if address:
            entry = self._by_address.get(address.lower().strip())

        if entry is None and name:
            name_lower = name.lower().strip()
            # Exact match first
            entry = self._by_name.get(name_lower)
            # Substring match fallback
            if entry is None:
                for key, candidate in self._by_name.items():
                    if name_lower in key or key in name_lower:
                        entry = candidate
                        break

        if entry is None:
            return _UNKNOWN

        confidence = "confirmed" if entry.contract_address_or_identifier else "likely"
        return BinanceSupportMetadata(
            binance_supported=entry.binance_supported,
            binance_list_type=entry.binance_list_type,
            support_confidence=confidence,
            support_source_url=entry.source_url or None,
            freshness=entry.verified_at or None,
            confidence=entry.source_reliability,
        )

    def lookup_dict(self, *, address: str | None = None, name: str | None = None) -> dict[str, Any]:
        meta = self.lookup(address=address, name=name)
        return {
            "binance_supported": meta.binance_supported,
            "binance_list_type": meta.binance_list_type,
            "support_confidence": meta.support_confidence,
            "support_source_url": meta.support_source_url,
            "freshness": meta.freshness,
            "confidence": meta.confidence,
        }
