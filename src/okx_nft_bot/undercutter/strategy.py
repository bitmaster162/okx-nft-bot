from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.config import CounterbidConfigManager
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.undercutter.state import ActiveOffer


@dataclass(slots=True)
class AttackTarget:
    collection: str
    suggested_price: float
    reason: str
    confidence: float


class UndercutStrategy:
    def __init__(self, settings: Settings, config_manager: CounterbidConfigManager) -> None:
        self.settings = settings
        self.config_mgr = config_manager

    def calculate_defense_price(self, *, our_price: float, competitor_price: float, collection: str) -> float:
        config = self.config_mgr.get_collection(collection)
        if config is None:
            raise RuntimeError(f"Missing collection config: {collection}")
        proposed = max(our_price, competitor_price) + self.settings.undercut_defense_margin_bnb
        return min(max(proposed, config.min_price_bnb), config.max_price_bnb)

    def should_withdraw(self, offer: ActiveOffer) -> bool:
        if offer.age_hours > self.settings.undercut_max_offer_age_hours:
            return True
        if offer.current_floor is not None and offer.price_bnb > offer.current_floor * 1.1:
            return True
        return False

    def find_attack_targets(
        self,
        *,
        tracked_collections: list[str],
        exclude: set[str],
        fetch_offers: Callable[[str], list[NormalizedOffer]],
    ) -> list[AttackTarget]:
        targets: list[AttackTarget] = []
        for collection in tracked_collections:
            normalized = collection.lower()
            if normalized in exclude:
                continue
            config = self.config_mgr.get_collection(normalized)
            if config is None or not config.enabled:
                continue
            offers = fetch_offers(normalized)
            best_offer = max((float(offer.price or 0.0) for offer in offers), default=0.0)
            if not offers:
                suggested = self._clamp_price(config.min_price_bnb + config.margin_bnb, config.min_price_bnb, config.max_price_bnb)
                targets.append(
                    AttackTarget(
                        collection=normalized,
                        suggested_price=suggested,
                        reason="No competitive offers found",
                        confidence=0.80,
                    )
                )
                continue
            threshold = config.min_price_bnb * self.settings.undercut_attack_ratio
            if best_offer < threshold:
                suggested = self._clamp_price(best_offer + config.margin_bnb, config.min_price_bnb, config.max_price_bnb)
                targets.append(
                    AttackTarget(
                        collection=normalized,
                        suggested_price=suggested,
                        reason=f"Weak best offer {best_offer:.6f} below threshold {threshold:.6f}",
                        confidence=0.65,
                    )
                )
        return targets

    @staticmethod
    def _clamp_price(value: float, min_price: float, max_price: float) -> float:
        return min(max(value, min_price), max_price)
