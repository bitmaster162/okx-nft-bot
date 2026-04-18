from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from okx_nft_bot.clients.okx import OKXMarketplaceClient
from okx_nft_bot.config import Settings
from okx_nft_bot.logging_utils import log_event
from okx_nft_bot.sniper.config import SniperTarget

logger = logging.getLogger(__name__)


@dataclass
class BuyResult:
    target_name: str
    token_id: str
    buy_price: float
    relist_price: float
    success: bool
    tx_hash: str | None = None
    error: str | None = None


class SniperEngine:
    """
    Monitors listings for a sniper target.
    If a listing appears below buy_below_price — buys it and relists at relist_price.

    Requirements:
      - BUYER_WALLET_ADDRESS in .env
      - BUYER_WALLET_PRIVATE_KEY in .env  (export from MetaMask)
      - Enough BNB on the wallet for purchase + gas
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OKXMarketplaceClient(settings)

    def run_once(self, target: SniperTarget) -> list[BuyResult]:
        """Single sniper cycle: scan listings → buy cheap → relist."""
        results: list[BuyResult] = []

        if not target.enabled:
            return results

        # 1. Fetch current listings
        listings = self._fetch_listings(target)
        if not listings:
            return results

        # 2. Filter by price
        cheap = [l for l in listings if self._listing_price(l) < target.buy_below_price]
        cheap.sort(key=lambda l: self._listing_price(l))

        log_event(
            'sniper_scan',
            target=target.name,
            total_listings=len(listings),
            cheap_listings=len(cheap),
            buy_below=target.buy_below_price,
        )

        # 3. Buy cheapest ones up to max_buys_per_cycle
        bought = 0
        for listing in cheap[:target.max_buys_per_cycle]:
            if bought >= target.max_buys_per_cycle:
                break
            result = self._buy_and_relist(target, listing)
            results.append(result)
            if result.success:
                bought += 1

        return results

    def _fetch_listings(self, target: SniperTarget) -> list[dict[str, Any]]:
        try:
            payload = self.client.get_listings(
                chain=target.chain,
                collection_address=target.collection_address,
                platform='okx',
                limit=20,
            )
            items = payload.get('data', [])
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
            return []
        except Exception as e:
            logger.error('sniper fetch_listings failed: %s', e)
            return []

    def _listing_price(self, listing: dict[str, Any]) -> float:
        try:
            price = listing.get('price') or listing.get('listingPrice') or 0
            return float(price)
        except (TypeError, ValueError):
            return 999999.0

    def _buy_and_relist(self, target: SniperTarget, listing: dict[str, Any]) -> BuyResult:
        token_id = str(listing.get('tokenId') or listing.get('token_id') or 'unknown')
        price = self._listing_price(listing)

        log_event('sniper_buy_attempt', target=target.name, token_id=token_id, price=price)

        # BUY
        buy_result = self._execute_buy(target, listing)
        if not buy_result['success']:
            return BuyResult(
                target_name=target.name,
                token_id=token_id,
                buy_price=price,
                relist_price=target.relist_price,
                success=False,
                error=buy_result.get('error'),
            )

        log_event('sniper_buy_success', target=target.name, token_id=token_id, price=price, tx=buy_result.get('tx_hash'))

        # RELIST
        relist_result = self._execute_relist(target, token_id)
        if not relist_result['success']:
            logger.warning('sniper: bought %s but relist failed: %s', token_id, relist_result.get('error'))

        log_event('sniper_relist', target=target.name, token_id=token_id, relist_price=target.relist_price, success=relist_result['success'])

        return BuyResult(
            target_name=target.name,
            token_id=token_id,
            buy_price=price,
            relist_price=target.relist_price,
            success=True,
            tx_hash=buy_result.get('tx_hash'),
        )

    def _execute_buy(self, target: SniperTarget, listing: dict[str, Any]) -> dict[str, Any]:
        """
        Execute purchase via OKX marketplace API.
        Requires BUYER_WALLET_PRIVATE_KEY in settings for on-chain signing.

        TODO: Implement on-chain signing via web3.py or OKX WaaS.
        """
        buyer_address = getattr(self.settings, 'buyer_wallet_address', None)
        buyer_key = getattr(self.settings, 'buyer_wallet_private_key', None)

        if not buyer_address or not buyer_key:
            return {'success': False, 'error': 'BUYER_WALLET_ADDRESS or BUYER_WALLET_PRIVATE_KEY not set in .env'}

        # TODO: Implement actual OKX NFT purchase
        # Steps:
        # 1. GET /api/v5/mktplace/nft/markets/listings?tokenId=X to get orderId
        # 2. POST /api/v5/mktplace/nft/markets/buy with orderId + buyer
        # 3. Sign resulting tx data with private key (web3.py)
        # 4. Submit signed tx to BSC network
        return {'success': False, 'error': 'NOT_IMPLEMENTED: need web3.py integration'}

    def _execute_relist(self, target: SniperTarget, token_id: str) -> dict[str, Any]:
        """
        List the purchased NFT at target.relist_price via OKX marketplace API.

        TODO: Implement Seaport order signing + OKX listing submission.
        """
        # TODO: Implement
        # Steps:
        # 1. Build Seaport order with price = target.relist_price
        # 2. Sign order with private key
        # 3. POST /api/v5/mktplace/nft/markets/list
        return {'success': False, 'error': 'NOT_IMPLEMENTED: need Seaport order signing'}
