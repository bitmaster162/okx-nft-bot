"""
Hot-reloadable stream filters for sales_stream daemon.

Reads config/stream_filters.json every cycle — edit the file live,
no restart needed.

Filters:
  - Global: min/max price, exclude zero, exclude self-trades
  - Collection blacklist: skip entire collections
  - Collection rules: per-collection price ranges and overrides
  - Chain whitelist/blacklist
  - Dust filter: skip sub-threshold trades within a collection
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("sales_stream.filters")


class StreamFilters:
    """Configurable trade filters with hot-reload from JSON."""

    def __init__(self, config_path: str | None = None):
        self.config_path = Path(
            config_path or os.getenv("STREAM_FILTERS_PATH", "./config/stream_filters.json")
        )
        self._config: dict = {}
        self._last_mtime: float = 0
        self._load()

    def _load(self):
        """Load or reload config from disk."""
        try:
            if not self.config_path.exists():
                log.debug("No filter config at %s — no filters active", self.config_path)
                self._config = {}
                return

            mtime = self.config_path.stat().st_mtime
            if mtime == self._last_mtime:
                return  # unchanged

            with open(self.config_path) as f:
                self._config = json.load(f)
            self._last_mtime = mtime
            log.info("Filters reloaded from %s", self.config_path)
        except Exception as exc:
            log.warning("Failed to load filters from %s: %s", self.config_path, exc)

    def reload(self):
        """Explicitly reload (called each cycle)."""
        self._load()

    # ── Access helpers ──────────────────────────────────────────

    @property
    def global_cfg(self) -> dict:
        return self._config.get("global", {})

    @property
    def collection_blacklist(self) -> set[str]:
        raw = self._config.get("collection_blacklist", [])
        return {a.lower() for a in raw if isinstance(a, str) and a.startswith("0x")}

    @property
    def collection_rules(self) -> dict[str, dict]:
        rules = self._config.get("collection_rules", {})
        return {k.lower(): v for k, v in rules.items() if k.startswith("0x")}

    @property
    def chain_whitelist(self) -> set[str]:
        raw = self._config.get("chain_whitelist", [])
        return {c.lower() for c in raw if c}

    @property
    def chain_blacklist(self) -> set[str]:
        raw = self._config.get("chain_blacklist", [])
        return {c.lower() for c in raw if c}

    # ── Filter logic ────────────────────────────────────────────

    def should_keep(self, event) -> bool:
        """Return True if event passes all filters, False to discard.

        Args:
            event: SaleEvent dataclass with fields:
                collection_address, chain, price, price_usd,
                seller, buyer, token_id, etc.
        """
        g = self.global_cfg

        # Chain filters
        chain = getattr(event, "chain", "").lower()
        cw = self.chain_whitelist
        if cw and chain not in cw:
            return False
        if chain in self.chain_blacklist:
            return False

        # Collection blacklist
        addr = getattr(event, "collection_address", "").lower()
        if addr in self.collection_blacklist:
            return False

        # Price filters (global)
        price = getattr(event, "price", 0)

        if g.get("exclude_zero_price", True) and price <= 0:
            return False

        min_price = g.get("min_price", 0)
        max_price = g.get("max_price", float("inf"))
        if price < min_price or price > max_price:
            return False

        # USD price filter
        price_usd = getattr(event, "price_usd", None)
        min_usd = g.get("min_price_usd", 0)
        if min_usd and price_usd is not None and price_usd < min_usd:
            return False

        # Self-trade filter (wash trading indicator)
        if g.get("exclude_self_trades", True):
            seller = getattr(event, "seller", "").lower()
            buyer = getattr(event, "buyer", "").lower()
            if seller and buyer and seller == buyer:
                return False

        # Per-collection rules
        rules = self.collection_rules.get(addr)
        if rules:
            if rules.get("enabled") is False:
                return False
            col_min = rules.get("min_price", 0)
            col_max = rules.get("max_price", float("inf"))
            if price < col_min or price > col_max:
                return False

        return True

    def get_fat_finger_threshold(self, collection_address: str) -> float:
        """Get fat-finger threshold for a collection (default from env)."""
        addr = collection_address.lower()
        rules = self.collection_rules.get(addr)
        if rules and "fat_finger_threshold" in rules:
            return float(rules["fat_finger_threshold"])
        return float(os.getenv("SNIPER_THRESHOLD", "0.5"))

    def filter_events(self, events: list) -> list:
        """Filter a batch of events, return only those passing all filters."""
        if not self._config:
            return events  # no config = no filtering
        kept = [e for e in events if self.should_keep(e)]
        filtered = len(events) - len(kept)
        if filtered:
            log.info("Filters: %d/%d trades passed (%d filtered out)",
                     len(kept), len(events), filtered)
        return kept
