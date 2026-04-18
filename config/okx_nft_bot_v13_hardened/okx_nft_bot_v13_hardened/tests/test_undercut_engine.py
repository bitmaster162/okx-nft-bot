from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from okx_nft_bot.config import Settings
from okx_nft_bot.counterbid.config import CounterbidConfigManager
from okx_nft_bot.normalizers.offers import NormalizedOffer
from okx_nft_bot.undercutter.engine import UndercutEngine
from okx_nft_bot.undercutter.state import PositionState


class FakeOfferClient:
    def __init__(self, offers_by_collection: dict[str, list[NormalizedOffer]]) -> None:
        self.offers_by_collection = offers_by_collection

    def fetch_offers(
        self,
        *,
        collection: str,
        chain: str,
        limit: int = 100,
        refresh: bool = False,
        maker: str | None = None,
        status: str = "active",
        max_pages: int | None = None,
    ) -> list[NormalizedOffer]:
        _ = chain, limit, refresh, maker, status, max_pages
        return self.offers_by_collection.get(collection.lower(), [])

    def submit_offer(self, signed_payload: dict[str, object]) -> dict[str, object]:
        return {
            "offer_id": "fake-live-offer",
            "status": "submitted",
            "raw": signed_payload,
        }

    def cancel_offer(self, offer_id: str) -> bool:
        _ = offer_id
        return True


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=True,
        buyer_wallet_address=None,
        buyer_wallet_private_key=None,
        undercut_defense_margin_bnb=0.0005,
        undercut_max_offer_age_hours=24,
    )


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
        buyer_wallet_address="0xbuyer",
        buyer_wallet_private_key="0xkey",
        telegram_bot_token="token",
        telegram_chat_id="123",
        undercut_defense_margin_bnb=0.0005,
        undercut_max_offer_age_hours=24,
    )


def _rate_limited_live_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
        buyer_wallet_address="0xbuyer",
        buyer_wallet_private_key="0xkey",
        telegram_bot_token="token",
        telegram_chat_id="123",
        undercut_defense_margin_bnb=0.0005,
        undercut_max_offer_age_hours=24,
        max_live_offers_per_hour=10,
        max_bnb_per_day=5.0,
        submit_cooldown_seconds=300,
    )


def _offer(*, offer_id: str, maker: str, price: float, collection: str) -> NormalizedOffer:
    return NormalizedOffer(
        market="okx",
        collection_slug_or_address=collection.lower(),
        chain="bsc",
        offer_id=offer_id,
        maker=maker.lower(),
        price=price,
        currency="WBNB",
        quantity=1,
        status="active",
        raw_payload_hash=f"hash-{offer_id}",
        observed_at=datetime.now(timezone.utc),
        source_type="collection_offer",
        source_reliability="high",
    )


def test_undercut_engine_defense_creates_new_active_offer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xabc", chain="bsc", min_price_bnb=0.1, max_price_bnb=1.0, margin_bnb=0.001)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="our-offer", collection="0xabc", chain="bsc", price_bnb=0.50)

    offers = {"0xabc": [_offer(offer_id="competitor", maker="0xenemy", price=0.60, collection="0xabc")]}
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient(offers),
        state=state,
        config_manager=manager,
    )

    actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "DEFENSE"
    assert actions[0].executed is True
    active = state.get_active_offers(chain="bsc")
    assert len(active) == 1
    assert active[0].order_hash != "our-offer"
    assert active[0].price_bnb > 0.60


def test_undercut_engine_attack_creates_position_when_collection_is_idle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xdef", chain="bsc", min_price_bnb=0.2, max_price_bnb=1.0, margin_bnb=0.01)
    state = PositionState(settings.execution_db_path)
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xdef": []}),
        state=state,
        config_manager=manager,
    )

    actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "ATTACK"
    assert actions[0].executed is True
    assert len(state.get_active_offers(chain="bsc")) == 1
    assert state.list_action_history(limit=5)[0]["action_type"] == "DRY_ATTACK"


def test_undercut_engine_withdraws_stale_offer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xabc", chain="bsc", min_price_bnb=0.1, max_price_bnb=1.0, margin_bnb=0.001)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="old-offer", collection="0xabc", chain="bsc", price_bnb=0.50)
    with sqlite3.connect(settings.execution_db_path) as conn:
        conn.execute("UPDATE active_offers SET placed_at = '2020-01-01T00:00:00+00:00' WHERE order_hash = 'old-offer'")

    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xabc": []}),
        state=state,
        config_manager=manager,
    )

    actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "WITHDRAW"
    assert actions[0].executed is True
    assert state.get_active_offers(chain="bsc") == []


def test_live_attack_calls_api(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xdef", chain="bsc", min_price_bnb=0.2, max_price_bnb=1.0, margin_bnb=0.01)
    state = PositionState(settings.execution_db_path)
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xdef": []}),
        state=state,
        config_manager=manager,
    )

    with patch("okx_nft_bot.undercutter.engine.preview_counterbid", return_value={"signature": "0xabc"}), \
         patch.object(engine.offer_client, "submit_offer", return_value={"offer_id": "live-1", "status": "submitted"}) as submit_mock, \
         patch.object(engine, "_send_live_notification", return_value=True) as notify_mock:
        actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "ATTACK"
    assert actions[0].executed is True
    assert actions[0].submit_result == {"offer_id": "live-1", "status": "submitted"}
    submit_mock.assert_called_once_with({"signature": "0xabc"})
    notify_mock.assert_called_once()
    assert state.list_action_history(limit=5)[0]["action_type"] == "LIVE_ATTACK"


def test_live_defense_cancels_old_offer_before_submit(tmp_path: Path) -> None:
    settings = _live_settings(tmp_path)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xabc", chain="bsc", min_price_bnb=0.1, max_price_bnb=1.0, margin_bnb=0.001)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="our-offer", collection="0xabc", chain="bsc", price_bnb=0.50)
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xabc": [_offer(offer_id="competitor", maker="0xenemy", price=0.60, collection="0xabc")]}),
        state=state,
        config_manager=manager,
    )

    call_order: list[str] = []

    def _cancel(offer_id: str, **_: object) -> bool:
        call_order.append(f"cancel:{offer_id}")
        return True

    def _submit(payload: dict[str, object]) -> dict[str, object]:
        _ = payload
        call_order.append("submit")
        return {"offer_id": "live-defense-1", "status": "submitted"}

    with patch("okx_nft_bot.undercutter.engine.preview_counterbid", return_value={"signature": "0xabc"}), \
         patch.object(engine.offer_client, "cancel_offer", side_effect=_cancel) as cancel_mock, \
         patch.object(engine.offer_client, "submit_offer", side_effect=_submit) as submit_mock, \
         patch.object(engine, "_send_live_notification", return_value=True):
        actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "DEFENSE"
    assert actions[0].executed is True
    assert call_order == ["cancel:our-offer", "submit"]
    cancel_mock.assert_called_once()
    submit_mock.assert_called_once()
    active = state.get_active_offers(chain="bsc")
    assert len(active) == 1
    assert active[0].order_hash == "live-defense-1"


def test_dry_defense_does_not_cancel_old_offer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xabc", chain="bsc", min_price_bnb=0.1, max_price_bnb=1.0, margin_bnb=0.001)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="our-offer", collection="0xabc", chain="bsc", price_bnb=0.50)
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xabc": [_offer(offer_id="competitor", maker="0xenemy", price=0.60, collection="0xabc")]}),
        state=state,
        config_manager=manager,
    )

    with patch.object(engine.offer_client, "cancel_offer") as cancel_mock:
        actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "DEFENSE"
    assert actions[0].executed is True
    cancel_mock.assert_not_called()


def test_dry_run_skips_submit_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xdef", chain="bsc", min_price_bnb=0.2, max_price_bnb=1.0, margin_bnb=0.01)
    state = PositionState(settings.execution_db_path)
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xdef": []}),
        state=state,
        config_manager=manager,
    )

    with patch.object(engine.offer_client, "submit_offer") as submit_mock:
        actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "ATTACK"
    assert actions[0].executed is True
    submit_mock.assert_not_called()
    assert state.list_action_history(limit=5)[0]["action_type"] == "DRY_ATTACK"


def test_live_attack_blocked_by_cooldown(tmp_path: Path) -> None:
    settings = _rate_limited_live_settings(tmp_path)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    manager = CounterbidConfigManager(settings.execution_db_path)
    manager.add_collection(address="0xdef", chain="bsc", min_price_bnb=0.2, max_price_bnb=1.0, margin_bnb=0.01)
    state = PositionState(settings.execution_db_path)
    state.record_submit_event(
        engine="undercutter",
        action_type="LIVE_ATTACK",
        collection="0xolder",
        chain="bsc",
        price_bnb=0.3,
        status="submitted",
        reason="seed",
    )
    engine = UndercutEngine(
        settings=settings,
        offer_client=FakeOfferClient({"0xdef": []}),
        state=state,
        config_manager=manager,
    )

    with patch("okx_nft_bot.undercutter.engine.preview_counterbid", return_value={"signature": "0xabc"}), \
         patch.object(engine.offer_client, "submit_offer") as submit_mock, \
         patch.object(engine, "_send_live_notification", return_value=True) as notify_mock:
        actions = engine.run_cycle()

    assert len(actions) == 1
    assert actions[0].action_type == "ATTACK"
    assert actions[0].executed is False
    assert "cooldown active" in (actions[0].error or "")
    submit_mock.assert_not_called()
    notify_mock.assert_called_once()
    assert state.list_action_history(limit=5)[0]["action_type"] == "LIVE_ATTACK"
