from __future__ import annotations

from okx_nft_bot.normalizers.offers import normalize_offer


def _hashless_offer(*, maker: str, price: str) -> dict[str, object]:
    return {
        "maker": {"address": maker},
        "current_price": price,
        "protocol_data": {
            "parameters": {
                "consideration": [
                    {
                        "itemType": 4,
                        "token": "collection-aa",
                        "identifierOrCriteria": "0",
                    }
                ]
            }
        },
    }


def test_hashless_opensea_offers_get_distinct_stable_fallback_ids() -> None:
    first_raw = _hashless_offer(
        maker="maker-one",
        price="1000000000000000000",
    )
    second_raw = _hashless_offer(
        maker="maker-two",
        price="2000000000000000000",
    )

    first = normalize_offer(first_raw, market="opensea", chain="eth")
    first_again = normalize_offer(first_raw, market="opensea", chain="eth")
    second = normalize_offer(second_raw, market="opensea", chain="eth")

    assert first.offer_id != "unknown"
    assert first.offer_id == first_again.offer_id
    assert first.offer_id != second.offer_id


def test_opensea_external_order_hash_remains_the_canonical_offer_id() -> None:
    raw = _hashless_offer(
        maker="maker-one",
        price="1000000000000000000",
    )
    raw["order_hash"] = "0xexternal-order-hash"

    offer = normalize_offer(raw, market="opensea", chain="eth")

    assert offer.offer_id == "0xexternal-order-hash"
