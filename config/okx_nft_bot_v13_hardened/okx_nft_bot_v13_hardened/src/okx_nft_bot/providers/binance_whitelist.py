from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Known Binance FAQ page URLs for NFT support lists
BINANCE_SOURCES = {
    "main": "https://www.binance.com/en/support/faq/3bee0ac7437649bc94b0903d1e22609f",
    "aggregator": "https://www.binance.com/en/support/faq/f5bd4e8935bf4d30abcfc9f5627710bb",
    # Bitcoin NFT support page — update URL when confirmed
    "bitcoin": "",
}

_STATIC_FALLBACK_PATH = Path(__file__).parent.parent.parent.parent.parent / "data" / "binance_whitelist.json"


class BinanceWhitelistEntry:
    __slots__ = (
        "collection_name",
        "contract_address_or_identifier",
        "network",
        "binance_supported",
        "binance_list_type",
        "source_url",
        "source_reliability",
        "verified_at",
        "extra",
    )

    def __init__(
        self,
        *,
        collection_name: str,
        contract_address_or_identifier: str,
        network: str,
        binance_supported: bool,
        binance_list_type: str,
        source_url: str,
        source_reliability: str = "reference_static",
        verified_at: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.collection_name = collection_name
        self.contract_address_or_identifier = contract_address_or_identifier.lower()
        self.network = network
        self.binance_supported = binance_supported
        self.binance_list_type = binance_list_type
        self.source_url = source_url
        self.source_reliability = source_reliability
        self.verified_at = verified_at
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_name": self.collection_name,
            "contract_address_or_identifier": self.contract_address_or_identifier,
            "network": self.network,
            "binance_supported": self.binance_supported,
            "binance_list_type": self.binance_list_type,
            "source_url": self.source_url,
            "source_reliability": self.source_reliability,
            "verified_at": self.verified_at,
            "extra": self.extra,
        }


class BinanceWhitelistProvider:
    """Parses Binance NFT support pages into a normalized static whitelist.

    Falls back gracefully to the bundled static JSON if fetching fails.
    Parse failures never crash the main pipeline.
    """

    def __init__(self, *, static_fallback_path: Path | None = None, timeout_seconds: int = 10) -> None:
        self._static_path = static_fallback_path or _STATIC_FALLBACK_PATH
        self._timeout = timeout_seconds

    def load(self) -> list[BinanceWhitelistEntry]:
        """Load whitelist, trying live fetch first, falling back to static data."""
        entries: list[BinanceWhitelistEntry] = []
        for list_type, url in BINANCE_SOURCES.items():
            if not url:
                logger.debug("Binance whitelist: no URL for list_type=%s, skipping live fetch", list_type)
                continue
            try:
                page_entries = self._fetch_and_parse(url=url, list_type=list_type)
                entries.extend(page_entries)
                logger.info("Binance whitelist: fetched %d entries from %s (%s)", len(page_entries), list_type, url)
            except Exception:
                logger.warning("Binance whitelist: live fetch failed for %s, will use static fallback", list_type, exc_info=True)

        if not entries:
            entries = self._load_static_fallback()
        return entries

    def load_static(self) -> list[BinanceWhitelistEntry]:
        """Load only from static fallback JSON — safe offline mode."""
        return self._load_static_fallback()

    def _fetch_and_parse(self, *, url: str, list_type: str) -> list[BinanceWhitelistEntry]:
        html = self._fetch_html(url)
        return _parse_html_entries(html, list_type=list_type, source_url=url)

    def _fetch_html(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; okx-nft-bot/0.13)"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")

    def _load_static_fallback(self) -> list[BinanceWhitelistEntry]:
        if not self._static_path.exists():
            logger.warning("Binance whitelist: static fallback not found at %s", self._static_path)
            return []
        try:
            with self._static_path.open(encoding="utf-8") as fh:
                raw_list = json.load(fh)
            entries = [_dict_to_entry(item) for item in raw_list if isinstance(item, dict)]
            logger.info("Binance whitelist: loaded %d entries from static fallback", len(entries))
            return entries
        except Exception:
            logger.exception("Binance whitelist: failed to load static fallback at %s", self._static_path)
            return []


def _parse_html_entries(html: str, *, list_type: str, source_url: str) -> list[BinanceWhitelistEntry]:
    """Best-effort extraction of NFT collection names and contract addresses from a Binance FAQ page."""
    verified_at = datetime.now(timezone.utc).date().isoformat()
    entries: list[BinanceWhitelistEntry] = []

    # Extract contract addresses using 0x pattern + surrounding text
    # Binance FAQ pages embed addresses in tables or list items
    address_pattern = re.compile(r'(0x[a-fA-F0-9]{40})', re.IGNORECASE)
    # Look for rows with name + address pairs
    table_row_pattern = re.compile(
        r'<tr[^>]*>.*?</tr>',
        re.IGNORECASE | re.DOTALL,
    )
    td_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
    strip_tags_pattern = re.compile(r'<[^>]+>')

    seen_ids: set[str] = set()

    for row_match in table_row_pattern.finditer(html):
        row_html = row_match.group(0)
        cells = [strip_tags_pattern.sub("", m.group(1)).strip() for m in td_pattern.finditer(row_html)]
        if len(cells) < 2:
            continue
        # Heuristic: first cell = name, subsequent cells may contain address or network
        name = cells[0]
        address_candidates = address_pattern.findall(" ".join(cells[1:]))
        if not name or not address_candidates:
            continue
        for addr in address_candidates:
            addr_lower = addr.lower()
            if addr_lower in seen_ids:
                continue
            seen_ids.add(addr_lower)
            network = _guess_network(cells, list_type)
            entries.append(BinanceWhitelistEntry(
                collection_name=name,
                contract_address_or_identifier=addr_lower,
                network=network,
                binance_supported=True,
                binance_list_type=list_type,
                source_url=source_url,
                source_reliability="reference_static",
                verified_at=verified_at,
            ))

    return entries


def _guess_network(cells: list[str], list_type: str) -> str:
    combined = " ".join(cells).lower()
    if "bsc" in combined or "bnb" in combined or "binance smart" in combined:
        return "bsc"
    if "bitcoin" in combined or "ordinals" in combined or list_type == "bitcoin":
        return "bitcoin"
    if "polygon" in combined or "matic" in combined:
        return "polygon"
    return "eth"


def _dict_to_entry(item: dict[str, Any]) -> BinanceWhitelistEntry:
    return BinanceWhitelistEntry(
        collection_name=str(item.get("collection_name", "unknown")),
        contract_address_or_identifier=str(item.get("contract_address_or_identifier", "")),
        network=str(item.get("network", "eth")),
        binance_supported=bool(item.get("binance_supported", True)),
        binance_list_type=str(item.get("binance_list_type", "main")),
        source_url=str(item.get("source_url", "")),
        source_reliability=str(item.get("source_reliability", "reference_static")),
        verified_at=str(item.get("verified_at", "")),
        extra=item.get("extra") or {},
    )
