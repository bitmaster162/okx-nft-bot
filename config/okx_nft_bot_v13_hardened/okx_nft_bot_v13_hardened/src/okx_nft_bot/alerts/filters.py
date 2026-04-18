from __future__ import annotations

from okx_nft_bot.config import Settings
from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.rules.rule_packs import RulePack


def evaluate_event(event: NFTEvent, settings: Settings, rule_packs: list[RulePack] | None = None) -> FilterDecision:
    reasons: list[str] = []
    matched_rules: list[str] = []

    if settings.collection_allowlist and event.collection not in settings.collection_allowlist:
        reasons.append("collection_not_allowlisted")

    if settings.min_price is not None and (event.price is None or event.price < settings.min_price):
        reasons.append("price_below_min")

    if settings.min_volume is not None and (event.volume_24h is None or event.volume_24h < settings.min_volume):
        reasons.append("volume_below_min")

    packs = rule_packs or []
    if packs:
        for pack in packs:
            if pack.matches(event):
                matched_rules.append(pack.name)
        if not matched_rules:
            reasons.append("no_rule_match")

    return FilterDecision(event_id=event.event_id, passed=not reasons, reasons=reasons, matched_rules=matched_rules)
