"""
counter_bidder.py  –  Parasite-Killer v15
===========================================
Counter-bidding logic with parasite detection and auto-undercut.

Main flow:
1. Fetch parasite offers for a collection (from OKX API)
2. Detect if parasite wallets are bidding
3. Calculate undercut price = parasite_price + margin_bnb
4. Validate against price limits (min/max)
5. Build + sign order via v14's seaport_signer
6. Batch process multiple collections
7. DRY_RUN=True by default; live submission only with explicit flag
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

# Assumes v14 seaport_signer is available (import from parent)
try:
    from seaport_signer import (
        build_order_payload,
        sign_order,
        get_counter,
        WBNB_ADDRESS,
    )
except ImportError:
    # Fallback for testing/development
    build_order_payload = None
    sign_order = None
    get_counter = None
    WBNB_ADDRESS = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

from config import ConfigManager, CollectionConfig
from okx_api import OKXAPIClient


logger = logging.getLogger(__name__)

# Parasite wallet addresses (v15 targets)
PARASITE_WALLETS = {
    "0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7".lower(),
    "0xf1771cf8831393422189330a79dd896223c357a4".lower(),
}


@dataclass
class CounterBidTask:
    """Single counter-bid task."""
    collection: str           # contract address
    parasite_offer_bnb: float # highest parasite offer (BNB)
    counter_price_bnb: float  # our counter-bid price (BNB)
    reason: str               # why this price?
    valid: bool               # passes all validation?
    error: Optional[str] = None  # if not valid, the reason


@dataclass
class BatchResult:
    """Result of batch counter-bid processing."""
    collection: str
    chain: str
    tasks: list[CounterBidTask]
    signed_orders: list[dict[str, Any]]
    submitted_count: int = 0
    failed_count: int = 0


class CounterBidder:
    """
    Counter-bidding engine.
    Detects parasite offers and auto-undercuts.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        okx_client: OKXAPIClient,
        offerer_address: str,
        private_key: str,
        rpc_url: str = "https://bsc-dataseed.binance.org/",
    ):
        self.config = config_manager
        self.okx = okx_client
        self.offerer = offerer_address
        self.private_key = private_key
        self.rpc_url = rpc_url

    def detect_parasite_offers(
        self,
        offers: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """
        Find the highest offer from a parasite wallet.

        Args:
            offers: list of offer dicts from OKX API

        Returns:
            Best parasite offer dict, or None if not found
        """
        parasite_offers = []

        for offer in offers:
            maker = offer.get("maker", "").lower()
            if maker in PARASITE_WALLETS:
                parasite_offers.append(offer)

        if not parasite_offers:
            return None

        # Return highest offer (by price)
        return max(
            parasite_offers,
            key=lambda o: float(o.get("price", 0)),
        )

    def calculate_counter_price(
        self,
        collection_config: CollectionConfig,
        parasite_offer_bnb: float,
    ) -> tuple[float, str]:
        """
        Calculate counter-bid price.

        Strategy:
        1. If parasite exists: max(parasite + margin, min_price)
        2. If no parasite: use min_price
        3. Clamp to [min_price, max_price]

        Returns:
            (price_bnb, reason_string)
        """
        min_price = collection_config.min_price_bnb
        max_price = collection_config.max_price_bnb
        margin = collection_config.margin_bnb

        if parasite_offer_bnb > 0:
            # Undercut: offer margin above parasite
            proposed = parasite_offer_bnb + margin
            reason = f"Parasite {parasite_offer_bnb:.6f} + margin {margin:.6f}"
        else:
            # No parasite; start at min
            proposed = min_price
            reason = f"No parasite; min price {min_price:.6f}"

        # Validate against limits
        if proposed < min_price:
            proposed = min_price
            reason += f" → clamped to min {min_price:.6f}"

        if proposed > max_price:
            proposed = max_price
            reason += f" → clamped to max {max_price:.6f}"

        return proposed, reason

    def validate_counter_bid(
        self,
        collection_config: CollectionConfig,
        counter_price_bnb: float,
        parasite_offer_bnb: float,
    ) -> tuple[bool, Optional[str]]:
        """
        Validate a counter-bid against all constraints.

        Returns:
            (is_valid, error_reason_or_none)
        """
        min_price = collection_config.min_price_bnb
        max_price = collection_config.max_price_bnb

        # Price must be within limits
        if counter_price_bnb < min_price:
            return False, f"Counter price {counter_price_bnb:.6f} < min {min_price:.6f}"

        if counter_price_bnb > max_price:
            return False, f"Counter price {counter_price_bnb:.6f} > max {max_price:.6f}"

        # If there's a parasite, we must be above it
        if parasite_offer_bnb > 0 and counter_price_bnb <= parasite_offer_bnb:
            return False, f"Counter {counter_price_bnb:.6f} not above parasite {parasite_offer_bnb:.6f}"

        return True, None

    def build_signed_counter_bid(
        self,
        collection: str,
        price_bnb: float,
        counter: int,
    ) -> dict[str, Any]:
        """
        Build and sign a Seaport order for counter-bid.
        Uses v14 seaport_signer functions.

        Args:
            collection: NFT contract address
            price_bnb: bid amount in BNB
            counter: Seaport counter value

        Returns:
            {parameters, signature} dict
        """
        if not build_order_payload or not sign_order:
            raise RuntimeError("seaport_signer not available")

        price_wei = int(price_bnb * 10**18)

        # Build payload
        payload = build_order_payload(
            offerer=self.offerer,
            collection=collection,
            price_wei=price_wei,
            counter=counter,
            duration_s=86_400,  # 24h
        )

        # Sign
        signature = sign_order(payload, self.private_key)

        return {
            "parameters": payload,
            "signature": signature,
        }

    def process_single_collection(
        self,
        collection_address: str,
        chain: str = "bsc",
        dry_run: bool = True,
    ) -> CounterBidTask:
        """
        Fetch offers for a collection, detect parasite, and generate counter-bid task.

        Returns:
            CounterBidTask (with or without error)
        """
        # Load config
        cfg = self.config.get_collection(collection_address)
        if not cfg:
            return CounterBidTask(
                collection=collection_address,
                parasite_offer_bnb=0,
                counter_price_bnb=0,
                reason="No config",
                valid=False,
                error=f"Collection {collection_address} not found in config",
            )

        if not cfg.enabled:
            return CounterBidTask(
                collection=collection_address,
                parasite_offer_bnb=0,
                counter_price_bnb=0,
                reason="Disabled",
                valid=False,
                error=f"Collection {collection_address} is disabled",
            )

        # Fetch offers
        try:
            offers = self.okx.fetch_offers(
                collection=collection_address,
                chain=chain,
                limit=100,
            )
        except Exception as e:
            return CounterBidTask(
                collection=collection_address,
                parasite_offer_bnb=0,
                counter_price_bnb=0,
                reason="Fetch failed",
                valid=False,
                error=str(e),
            )

        # Detect parasite
        parasite_offer_dict = self.detect_parasite_offers(offers)
        parasite_price_bnb = 0.0

        if parasite_offer_dict:
            try:
                parasite_price_bnb = float(parasite_offer_dict.get("price", 0))
            except (ValueError, TypeError):
                parasite_price_bnb = 0.0

        # Calculate counter price
        counter_price_bnb, reason = self.calculate_counter_price(cfg, parasite_price_bnb)

        # Validate
        is_valid, error_msg = self.validate_counter_bid(cfg, counter_price_bnb, parasite_price_bnb)

        return CounterBidTask(
            collection=collection_address,
            parasite_offer_bnb=parasite_price_bnb,
            counter_price_bnb=counter_price_bnb,
            reason=reason,
            valid=is_valid,
            error=error_msg,
        )

    def process_batch(
        self,
        chain: str = "bsc",
        dry_run: bool = True,
    ) -> BatchResult:
        """
        Process all enabled collections for a chain.

        Returns:
            BatchResult with tasks and signed orders
        """
        collections = self.config.list_collections(chain=chain, enabled_only=True)

        if not collections:
            logger.warning(f"No enabled collections for {chain}")
            return BatchResult(
                collection="[batch]",
                chain=chain,
                tasks=[],
                signed_orders=[],
            )

        tasks = []
        signed_orders = []

        for cfg in collections:
            logger.info(f"Processing {cfg.address} on {chain}")

            # Generate counter-bid task
            task = self.process_single_collection(cfg.address, chain, dry_run)
            tasks.append(task)

            # If valid, build signed order
            if task.valid:
                try:
                    counter = get_counter(self.offerer, self.rpc_url)
                    signed = self.build_signed_counter_bid(
                        cfg.address,
                        task.counter_price_bnb,
                        counter,
                    )
                    signed_orders.append(signed)
                    logger.info(f"  ✓ Valid bid: {task.counter_price_bnb:.6f} BNB (parasite: {task.parasite_offer_bnb:.6f})")
                except Exception as e:
                    task.valid = False
                    task.error = str(e)
                    logger.error(f"  ✗ Failed to sign: {e}")
            else:
                logger.warning(f"  ✗ Invalid: {task.error}")

        return BatchResult(
            collection="[batch]",
            chain=chain,
            tasks=tasks,
            signed_orders=signed_orders,
        )

    def submit_batch(
        self,
        batch_result: BatchResult,
        dry_run: bool = True,
    ) -> BatchResult:
        """
        Submit all signed orders in the batch.

        Modifies batch_result.submitted_count and .failed_count.

        Args:
            batch_result: from process_batch()
            dry_run: if False, actually submit (requires explicit user intent)

        Returns:
            Updated batch_result
        """
        for signed_order in batch_result.signed_orders:
            try:
                response = self.okx.submit_offer(signed_order, dry_run=dry_run)
                if response.get("code") == "0":
                    batch_result.submitted_count += 1
                    logger.info(f"  ✓ Submitted")
                else:
                    batch_result.failed_count += 1
                    logger.error(f"  ✗ API error: {response}")
            except Exception as e:
                batch_result.failed_count += 1
                logger.error(f"  ✗ Submit failed: {e}")

        return batch_result
