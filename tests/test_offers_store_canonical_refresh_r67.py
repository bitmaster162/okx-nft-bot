from __future__ import annotations

from datetime import datetime, timezone

from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore


def _offer(
    *,
    token_id: str | None,
    source_type: str,
    collection: str,
    maker: str,
    currency: str,
    quantity: int,
    price: float,
    raw_hash: str,
    observed_at: datetime,
) -> NormalizedOffer:
    return NormalizedOffer(
        market="okx",
        collection_slug_or_address=collection,
        chain="bsc",
        token_id=token_id,
        offer_id="r67-order-1",
        maker=maker,
        price=price,
        currency=currency,
        quantity=quantity,
        status="active",
        created_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
        raw_payload_hash=raw_hash,
        observed_at=observed_at,
        source_type=source_type,
        source_reliability="high",
    )


def test_upsert_refreshes_canonical_fields_for_existing_offer_id(tmp_path) -> None:
    store = OffersStore(tmp_path / "offers.db")

    legacy = _offer(
        token_id=None,
        source_type="collection_offer",
        collection="0xold",
        maker="0xoldmaker",
        currency="OLD",
        quantity=1,
        price=1.0,
        raw_hash="legacy",
        observed_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
    )
    corrected = _offer(
        token_id="0",
        source_type="token_offer",
        collection="0xcorrected",
        maker="0xnewmaker",
        currency="WBNB",
        quantity=2,
        price=1.25,
        raw_hash="corrected",
        observed_at=datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc),
    )

    assert store.upsert_offers([legacy]) == 1
    assert store.upsert_offers([corrected]) == 1

    rows = store.query_offers(OfferFilters(limit=10))
    assert len(rows) == 1
    current = rows[0]

    assert current.offer_id == corrected.offer_id
    assert current.market == corrected.market
    assert current.chain == corrected.chain
    assert current.collection_slug_or_address == "0xcorrected"
    assert current.token_id == "0"
    assert current.maker == "0xnewmaker"
    assert current.price == 1.25
    assert current.currency == "WBNB"
    assert current.quantity == 2
    assert current.source_type == "token_offer"
    assert current.source_reliability == "high"
    assert current.raw_payload_hash == "corrected"
    assert current.observed_at == corrected.observed_at
