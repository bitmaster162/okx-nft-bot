from __future__ import annotations

from okx_nft_bot.mass_offer import MassOfferTracker
from okx_nft_bot.mass_offer.campaign_status_safety import install_mass_offer_campaign_status_safety


def _campaign_id(tracker: MassOfferTracker) -> int:
    return tracker.start_campaign(
        collection="0x1111111111111111111111111111111111111111",
        chain="bsc",
        price_bnb=1.0,
        duration_hours=24,
        delay_seconds=0.0,
        dry_run=True,
        rarity_filter=(),
        unlisted_only=False,
        exclude_own=True,
        max_existing_offer=None,
        min_token_id=None,
        max_token_id=None,
        max_total=10,
    )


def test_campaign_cancel_is_suppressed_while_active_item_remains(tmp_path) -> None:
    tracker = MassOfferTracker(tmp_path / "mass_offer.db")
    campaign_id = _campaign_id(tracker)
    tracker.record_item(
        campaign_id=campaign_id,
        collection="0x1111111111111111111111111111111111111111",
        chain="bsc",
        token_id=1,
        owner=None,
        rarity=None,
        listed=False,
        existing_offer_bnb=None,
        price_bnb=1.0,
        status="active",
    )

    tracker.mark_campaign_status(campaign_id=campaign_id, status="cancelled")

    campaign = tracker.list_campaigns(limit=1)[0]
    assert campaign.campaign_id == campaign_id
    assert campaign.status == "running"


def test_campaign_can_be_cancelled_after_last_active_item_is_closed(tmp_path) -> None:
    tracker = MassOfferTracker(tmp_path / "mass_offer.db")
    campaign_id = _campaign_id(tracker)
    tracker.record_item(
        campaign_id=campaign_id,
        collection="0x1111111111111111111111111111111111111111",
        chain="bsc",
        token_id=1,
        owner=None,
        rarity=None,
        listed=False,
        existing_offer_bnb=None,
        price_bnb=1.0,
        status="cancelled",
    )

    tracker.mark_campaign_status(campaign_id=campaign_id, status="cancelled")

    campaign = tracker.list_campaigns(limit=1)[0]
    assert campaign.status == "cancelled"


def test_r63_installer_is_active_and_idempotent() -> None:
    current = MassOfferTracker.mark_campaign_status
    assert getattr(current, "_r63_campaign_status_guard", False) is True

    install_mass_offer_campaign_status_safety(MassOfferTracker)

    assert MassOfferTracker.mark_campaign_status is current
