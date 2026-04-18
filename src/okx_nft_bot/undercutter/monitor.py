from __future__ import annotations

from okx_nft_bot.counterbid.okx_api import OKXAPIClient
from okx_nft_bot.normalizers.offers import NormalizedOffer


class OfferMonitor:
    def __init__(self, offer_client: OKXAPIClient) -> None:
        self.offer_client = offer_client

    def fetch_offers(
        self,
        collection: str,
        *,
        chain: str,
        refresh: bool = False,
        limit: int = 100,
    ) -> list[NormalizedOffer]:
        return self.offer_client.fetch_offers(
            collection=collection,
            chain=chain,
            refresh=refresh,
            limit=limit,
        )

    @staticmethod
    def get_best_offer(offers: list[NormalizedOffer]) -> NormalizedOffer | None:
        if not offers:
            return None
        return max(offers, key=lambda offer: float(offer.price or 0.0))

    @staticmethod
    def get_best_offer_excluding(offers: list[NormalizedOffer], wallet_address: str | None) -> NormalizedOffer | None:
        excluded = (wallet_address or "").lower()
        filtered = [offer for offer in offers if (offer.maker or "").lower() != excluded]
        return OfferMonitor.get_best_offer(filtered)
