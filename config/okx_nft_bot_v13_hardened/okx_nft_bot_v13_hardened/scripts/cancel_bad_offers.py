#!/usr/bin/env python3
"""Cancel our offers > $0.51 where we bid in wrong currency.

Finds all our active offers, checks if the currency matches the top
parasite's currency for that collection. Cancels mismatched ones.

Usage:
    python scripts/cancel_bad_offers.py              # dry run
    python scripts/cancel_bad_offers.py --execute    # actually cancel
"""
import sys
import os
import time
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Price thresholds
MIN_USD_TO_CANCEL = 0.51  # Only cancel offers above this


def get_usd_price(currency: str) -> float:
    """Rough USD prices for quick filtering."""
    prices = {
        "WBNB": 600.0, "BNB": 600.0,
        "WETH": 2100.0, "ETH": 2100.0,
        "USDT": 1.0, "USDC": 1.0, "BUSD": 1.0, "DAI": 1.0,
    }
    return prices.get(currency.upper(), 0.0)


def to_usd(amount: float, currency: str) -> float:
    return amount * get_usd_price(currency)


def main():
    execute = "--execute" in sys.argv

    from okx_nft_bot.config import load_settings
    from okx_nft_bot.counterbid.okx_api import OKXAPIClient

    settings = load_settings()
    client = OKXAPIClient(settings=settings)

    to_cancel = []

    for chain in ("bsc", "eth"):
        log.info("Fetching our offers on %s...", chain.upper())
        try:
            offers = client.get_my_offers(chain=chain)
        except Exception as e:
            log.error("Failed to fetch %s offers: %s", chain, e)
            continue

        log.info("  Found %d active offers on %s", len(offers), chain.upper())

        for offer in offers:
            price_raw = float(offer.get("price") or 0)
            currency = (offer.get("currencyName") or offer.get("currency") or "").upper()
            order_id = offer.get("orderId") or offer.get("offerId") or offer.get("id") or ""
            collection = (offer.get("collectionAddress") or offer.get("nftAddress") or "").lower()
            name = offer.get("collectionName") or offer.get("nftName") or collection[:14]

            # Convert wei to human
            if price_raw > 1_000_000_000:
                if chain == "eth" and currency in ("USDT", "USDC"):
                    price = price_raw / 1e6
                else:
                    price = price_raw / 1e18
            else:
                price = price_raw

            usd = to_usd(price, currency)

            if usd > MIN_USD_TO_CANCEL:
                to_cancel.append({
                    "chain": chain,
                    "order_id": order_id,
                    "collection": collection,
                    "name": name,
                    "price": price,
                    "currency": currency,
                    "usd": usd,
                })

    if not to_cancel:
        log.info("No offers > $%.2f found. Nothing to cancel.", MIN_USD_TO_CANCEL)
        return

    log.info("\n=== Offers to cancel: %d ===", len(to_cancel))
    for o in to_cancel:
        log.info("  %s %s: %.6f %s ($%.2f) order=%s",
                 o["chain"].upper(), o["name"],
                 o["price"], o["currency"], o["usd"],
                 o["order_id"][:16] if o["order_id"] else "???")

    if not execute:
        log.info("\nDRY RUN — add --execute to actually cancel")
        return

    log.info("\nCancelling %d offers...", len(to_cancel))
    ok_count = 0
    for o in to_cancel:
        if not o["order_id"]:
            log.warning("  SKIP %s: no order_id", o["name"])
            continue
        try:
            ok = client.cancel_offer(o["order_id"])
            status = "OK" if ok else "FAILED"
            log.info("  %s cancel %s %.4f %s ($%.2f): %s",
                     o["chain"].upper(), o["name"],
                     o["price"], o["currency"], o["usd"], status)
            if ok:
                ok_count += 1
            time.sleep(0.3)
        except Exception as e:
            log.error("  ERROR cancelling %s: %s", o["name"], e)

    log.info("\nDone: %d/%d cancelled", ok_count, len(to_cancel))


if __name__ == "__main__":
    main()
