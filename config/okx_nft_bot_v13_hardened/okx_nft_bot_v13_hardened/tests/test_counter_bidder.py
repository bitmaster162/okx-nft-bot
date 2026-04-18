from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from eth_account import Account

from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid import CounterBidder, CounterbidConfigManager
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.storage.offers_store import OffersStore
from okx_nft_bot.undercutter.state import PositionState

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)
PARASITE = "0x8389a3e684d532a3b0b4cdbb72c9c7797e6fa4f7"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=True,
        parasite_wallets=(PARASITE,),
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
    )


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
        parasite_wallets=(PARASITE,),
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
        telegram_bot_token="token",
        telegram_chat_id="123",
    )


def _rate_limited_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
        parasite_wallets=(PARASITE,),
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
        telegram_bot_token="token",
        telegram_chat_id="123",
        max_live_offers_per_hour=1,
        max_bnb_per_day=5.0,
        submit_cooldown_seconds=0,
    )


def _offer(*, offer_id: str, maker: str, price: float, collection: str = "0xcollection") -> NormalizedOffer:
    return NormalizedOffer(
        market="okx",
        collection_slug_or_address=collection.lower(),
        chain="bsc",
        token_id=None,
        offer_id=offer_id,
        maker=maker.lower(),
        price=price,
        currency="WBNB",
        quantity=1,
        status="active",
        created_at=datetime.now(timezone.utc),
        expires_at=None,
        raw_payload_hash=f"hash-{offer_id}",
        observed_at=datetime.now(timezone.utc),
        source_type="collection_offer",
        source_reliability="high",
    )


def test_counterbid_config_manager_roundtrip(tmp_path: Path) -> None:
    manager = CounterbidConfigManager(tmp_path / "execution.sqlite3")
    added = manager.add_collection(
        address="0xABC123",
        chain="bsc",
        min_price_bnb=0.01,
        max_price_bnb=1.0,
        margin_bnb=0.001,
    )
    assert added.address == "0xabc123"
    assert manager.get_collection("0xABC123") is not None
    assert manager.disable_collection("0xabc123") is True
    assert manager.enable_collection("0xabc123") is True
    assert len(manager.describe()) == 1


def test_counter_bidder_detects_and_prices_parasite_offer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers(
        [
            _offer(offer_id="o1", maker="0xnormal", price=0.40),
            _offer(offer_id="o2", maker=PARASITE, price=0.50),
        ]
    )
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xcollection",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    task, refresh_result = bidder.process_single_collection("0xcollection", sign_preview=False)

    assert refresh_result is None
    assert task.valid is True
    assert task.parasite_maker == PARASITE
    assert round(task.parasite_offer_bnb, 6) == 0.5
    assert round(task.counter_price_bnb, 6) == 0.501


def test_counter_bidder_can_attach_signed_preview(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers([_offer(offer_id="o2", maker=PARASITE, price=0.50)])
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xcollection",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    with patch.object(bidder, "build_signed_counter_bid", return_value={"signature": "0xabc"}):
        task, _ = bidder.process_single_collection("0xcollection", sign_preview=True)

    assert task.valid is True
    assert task.preview_payload == {"signature": "0xabc"}


def test_counter_bidder_batch_uses_enabled_configs_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers([_offer(offer_id="o2", maker=PARASITE, price=0.50, collection="0xenabled")])
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xenabled",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
        enabled=True,
    )
    manager.add_collection(
        address="0xdisabled",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
        enabled=False,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    result = bidder.process_batch()

    assert result.valid_count == 1
    assert len(result.tasks) == 1
    assert result.tasks[0].collection == "0xenabled"


def test_live_submit_calls_api(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers([_offer(offer_id="o2", maker=PARASITE, price=0.50)])
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xcollection",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    with patch.object(bidder, "build_signed_counter_bid", return_value={"signature": "0xabc"}), \
         patch.object(bidder.okx, "submit_offer", return_value={"offer_id": "offer-1", "status": "submitted"}) as submit_mock, \
         patch.object(bidder, "_send_live_notification", return_value=True) as notify_mock:
        task, _ = bidder.process_single_collection("0xcollection", sign_preview=True)

    assert task.valid is True
    assert task.action_type == "LIVE_COUNTERBID"
    assert task.submit_result == {"offer_id": "offer-1", "status": "submitted"}
    submit_mock.assert_called_once_with({"signature": "0xabc"})
    notify_mock.assert_called_once()
    active = PositionState(settings.execution_db_path).get_active_offers(chain="bsc")
    assert len(active) == 1
    assert active[0].order_hash == "offer-1"
    assert active[0].price_bnb == task.counter_price_bnb
    assert active[0].current_floor == task.parasite_offer_bnb


def test_dry_run_skips_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers([_offer(offer_id="o2", maker=PARASITE, price=0.50)])
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xcollection",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    with patch.object(bidder, "build_signed_counter_bid", return_value={"signature": "0xabc"}), \
         patch.object(bidder.okx, "submit_offer") as submit_mock:
        task, _ = bidder.process_single_collection("0xcollection", sign_preview=True)

    assert task.valid is True
    assert task.action_type == "DRY_COUNTERBID"
    assert task.submit_result is None
    submit_mock.assert_not_called()
    active = PositionState(settings.execution_db_path).get_active_offers(chain="bsc")
    assert len(active) == 1
    assert active[0].order_hash.startswith("dryrun-cb-")
    assert active[0].price_bnb == task.counter_price_bnb
    assert active[0].current_floor == task.parasite_offer_bnb


def test_live_counterbid_blocked_by_rate_limit(tmp_path: Path) -> None:
    settings = _rate_limited_settings(tmp_path)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    store = OffersStore(settings.offers_db_path)
    store.upsert_offers([_offer(offer_id="o2", maker=PARASITE, price=0.50)])
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(
        address="0xcollection",
        chain="bsc",
        min_price_bnb=0.10,
        max_price_bnb=1.00,
        margin_bnb=0.001,
    )

    bidder = CounterBidder(settings=settings, config_manager=manager)
    bidder.state.record_submit_event(
        engine="counterbid",
        action_type="LIVE_COUNTERBID",
        collection="0xcollection",
        chain="bsc",
        price_bnb=0.4,
        status="submitted",
        reason="seed",
    )
    with patch.object(bidder, "build_signed_counter_bid", return_value={"signature": "0xabc"}), \
         patch.object(bidder.okx, "submit_offer") as submit_mock, \
         patch.object(bidder, "_send_live_notification", return_value=True) as notify_mock:
        task, _ = bidder.process_single_collection("0xcollection", sign_preview=True)

    assert task.valid is False
    assert task.action_type == "LIVE_COUNTERBID_BLOCKED"
    assert "rate limit hit" in (task.error or "")
    submit_mock.assert_not_called()
    notify_mock.assert_called_once()
