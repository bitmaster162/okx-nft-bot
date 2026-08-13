from __future__ import annotations

from datetime import datetime, timezone

from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.storage.offers_store import OfferFilters, OffersStore


def _dt(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=timezone.utc)


def test_upsert_refreshes_all_structured_fields_for_existing_offer(tmp_path) -> None:
    store = OffersStore(tmp_path / "offers.sqlite3")

    original = NormalizedOffer(
        market="okx",
        collection_slug_or_address="old-collection",
        chain="eth",
        token_id=None,
        offer_id="offer-semantic-refresh-r67",
        maker="old-maker",
        price=1.0,
        currency="ETH",
        quantity=1,
        status="active",
        created_at=_dt(1),
        expires_at=_dt(10),
        raw_payload_hash="old-hash",
        observed_at=_dt(2),
        source_type="collection_offer",
        source_reliability="low",
    )
    corrected = NormalizedOffer(
        market="okx",
        collection_slug_or_address="new-collection",
        chain="bsc",
        token_id="0",
        offer_id="offer-semantic-refresh-r67",
        maker="new-maker",
        price=2.5,
        currency="WETH",
        quantity=3,
        status="cancelled",
        created_at=_dt(3),
        expires_at=_dt(11),
        raw_payload_hash="new-hash",
        observed_at=_dt(4),
        source_type="token_offer",
        source_reliability="high",
    )

    assert store.upsert_offers([original]) == 1
    assert store.upsert_offers([corrected]) == 1

    stored = store.query_offers(OfferFilters(limit=10))
    assert len(stored) == 1
    assert stored[0].model_dump() == corrected.model_dump()
