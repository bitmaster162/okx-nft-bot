from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.config import CounterbidConfigManager
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.undercutter.state import ActiveOffer
from okx_nft_bot.undercutter.strategy import UndercutStrategy


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_chain="bsc",
        undercut_attack_ratio=0.75,
        undercut_defense_margin_bnb=0.0005,
        undercut_max_offer_age_hours=24,
    )


def _offer(price: float) -> NormalizedOffer:
    return NormalizedOffer(
        market="okx",
        collection_slug_or_address="0xabc",
        chain="bsc",
        offer_id=f"offer-{price}",
        maker="0xmaker",
        price=price,
        currency="WBNB",
        quantity=1,
        status="active",
        raw_payload_hash=f"hash-{price}",
        observed_at=datetime.now(timezone.utc),
        source_type="collection_offer",
        source_reliability="high",
    )


def test_calculate_defense_price_respects_collection_bounds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xabc",
        chain="bsc",
        min_price_bnb=0.1,
        max_price_bnb=0.6,
        margin_bnb=0.001,
    )
    strategy = UndercutStrategy(settings, manager)
    price = strategy.calculate_defense_price(our_price=0.5, competitor_price=0.7, collection="0xabc")
    assert price == 0.6


def test_should_withdraw_on_age_or_overpay(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    strategy = UndercutStrategy(settings, manager)

    stale_offer = ActiveOffer(
        order_hash="o1",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.5,
        status="active",
        placed_at=datetime.now(timezone.utc) - timedelta(hours=30),
        last_checked_at=datetime.now(timezone.utc),
    )
    assert strategy.should_withdraw(stale_offer) is True

    overpay_offer = ActiveOffer(
        order_hash="o2",
        collection="0xabc",
        chain="bsc",
        price_bnb=1.2,
        status="active",
        placed_at=datetime.now(timezone.utc),
        last_checked_at=datetime.now(timezone.utc),
        current_floor=1.0,
    )
    assert strategy.should_withdraw(overpay_offer) is True


def test_find_attack_targets_when_market_is_empty_or_weak(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xabc",
        chain="bsc",
        min_price_bnb=0.2,
        max_price_bnb=1.0,
        margin_bnb=0.01,
    )
    manager.add_collection(
        address="0xdef",
        chain="bsc",
        min_price_bnb=0.2,
        max_price_bnb=1.0,
        margin_bnb=0.01,
    )
    strategy = UndercutStrategy(settings, manager)

    offers_by_collection = {
        "0xabc": [],
        "0xdef": [_offer(0.05)],
    }
    targets = strategy.find_attack_targets(
        tracked_collections=["0xabc", "0xdef"],
        exclude=set(),
        fetch_offers=lambda collection: offers_by_collection[collection],
    )
    assert {target.collection for target in targets} == {"0xabc", "0xdef"}
