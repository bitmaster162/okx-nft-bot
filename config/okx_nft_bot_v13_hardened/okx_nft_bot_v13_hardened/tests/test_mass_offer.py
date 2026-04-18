from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from eth_account import Account

from okx_nft_bot.cli import cmd_mass_offer, cmd_mass_offer_cancel, cmd_mass_offer_status
from okx_nft_bot.config import Settings
from okx_nft_bot.mass_offer.engine import MassOfferEngine, MassOfferRunResult
from okx_nft_bot.mass_offer.scanner import MassOfferFilters, select_mass_offer_targets
from okx_nft_bot.undercutter.state import PositionState

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ACCOUNT = Account.from_key(TEST_PRIVATE_KEY)
TEST_COLLECTION = "0x1234567890123456789012345678901234567890"


class FakeMarketClient:
    def __init__(self, assets: list[dict[str, object]]) -> None:
        self.assets = assets
        self.calls: list[dict[str, object]] = []

    def get_nft_list(self, *, chain: str, contract_address: str, limit: int | None = None, cursor: str | None = None):
        self.calls.append(
            {
                "chain": chain,
                "contract_address": contract_address,
                "limit": limit,
                "cursor": cursor,
            }
        )
        return {"data": {"data": self.assets}}


class FakeOffersProvider:
    def __init__(self, offers=None) -> None:
        self.offers = offers or []
        self.calls: list[dict[str, object]] = []

    def fetch_all_pages(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.offers)


class FakeAPIClient:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []
        self.cancel_calls: list[dict[str, object]] = []
        self.my_offers: list[dict[str, object]] = []

    def submit_offer(self, payload: dict[str, object]) -> dict[str, object]:
        self.submissions.append(payload)
        return {"offer_id": f"offer-{len(self.submissions)}", "status": "open"}

    def create_offer(
        self,
        *,
        chain: str,
        wallet_address: str,
        collection_address: str,
        token_id: str,
        price_raw: str,
        currency_address: str,
        valid_time: int,
        count: int | None = None,
    ) -> dict[str, object]:
        payload = {
            "chain": chain,
            "wallet_address": wallet_address,
            "collection_address": collection_address,
            "token_id": token_id,
            "price_raw": price_raw,
            "currency_address": currency_address,
            "valid_time": valid_time,
            "count": count,
        }
        self.submissions.append(payload)
        return {"offer_id": f"offer-{len(self.submissions)}", "status": "open"}

    def cancel_offer(
        self,
        offer_id: str,
        *,
        chain: str = "bsc",
        order_params: dict[str, object] | None = None,
    ) -> bool:
        self.cancel_calls.append(
            {
                "offer_id": offer_id,
                "chain": chain,
                "order_params": order_params,
            }
        )
        return True

    def get_my_offers(self, chain: str = "bsc", **_: object) -> list[dict[str, object]]:
        return list(self.my_offers)


def _settings(tmp_path: Path, *, dry_run: bool = True, mass_offer_dry_run: bool = True) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
        dry_run=dry_run,
        execution_chain="bsc",
        mass_offer_dry_run=mass_offer_dry_run,
    )


def test_select_mass_offer_targets_filters_rarity_unlisted_and_own_wallet() -> None:
    assets = [
        {
            "tokenId": "1",
            "ownerAddress": "0xaaa",
            "saleStatus": "unlisted",
            "traits": [{"traitType": "rarity", "value": "R"}],
        },
        {
            "tokenId": "2",
            "ownerAddress": "0xbbb",
            "saleStatus": "unlisted",
            "traits": [{"traitType": "rarity", "value": "N"}],
        },
        {
            "tokenId": "3",
            "ownerAddress": "0xccc",
            "listed": True,
            "traits": [{"traitType": "rarity", "value": "SR"}],
        },
        {
            "tokenId": "4",
            "ownerAddress": TEST_ACCOUNT.address,
            "saleStatus": "unlisted",
            "traits": [{"traitType": "rarity", "value": "SSR"}],
        },
        {
            "tokenId": "5",
            "ownerAddress": "0xddd",
            "saleStatus": "unlisted",
            "traits": [{"traitType": "rarity", "value": "SSR"}],
        },
    ]

    result = select_mass_offer_targets(
        assets,
        filters=MassOfferFilters(
            rarity_filter=("R", "SR", "SSR"),
            unlisted_only=True,
            exclude_own=True,
            max_existing_offer=0.02,
        ),
        own_wallet=TEST_ACCOUNT.address,
        existing_offer_prices={"5": 0.03},
    )

    assert [item.token_id for item in result.targets] == [1]
    assert {item.reason for item in result.skipped} == {
        "rarity_mismatch",
        "listed",
        "owned_by_buyer",
        "existing_offer_above_max",
    }


def test_mass_offer_engine_dry_run_does_not_submit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=True, mass_offer_dry_run=True)
    market_client = FakeMarketClient(
        [
            {
                "tokenId": "11",
                "ownerAddress": "0xaaa",
                "saleStatus": "unlisted",
                "traits": [{"traitType": "rarity", "value": "R"}],
            }
        ]
    )
    api_client = FakeAPIClient()
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=lambda _: None,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=7):
        result = engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.01,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=True,
        )

    assert result.dry_run is True
    assert result.dry_run_count == 1
    assert result.submitted_count == 0
    assert api_client.submissions == []


def test_mass_offer_engine_rate_limits_live_submits(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
        dry_run=False,
        execution_chain="bsc",
        mass_offer_dry_run=False,
        submit_cooldown_seconds=0,
    )
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    market_client = FakeMarketClient(
        [
            {"tokenId": "1", "ownerAddress": "0xaaa", "saleStatus": "unlisted", "rarity": "R"},
            {"tokenId": "2", "ownerAddress": "0xbbb", "saleStatus": "unlisted", "rarity": "R"},
            {"tokenId": "3", "ownerAddress": "0xccc", "saleStatus": "unlisted", "rarity": "R"},
        ]
    )
    api_client = FakeAPIClient()
    sleep_calls: list[float] = []
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=sleep_calls.append,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=9):
        result = engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.02,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=False,
            delay_seconds=0.5,
        )

    assert result.dry_run is False
    assert result.submitted_count == 3
    assert len(api_client.submissions) == 3
    assert sleep_calls == [0.5, 0.5]


def test_mass_offer_engine_live_updates_execution_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=False, mass_offer_dry_run=False)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    market_client = FakeMarketClient(
        [
            {"tokenId": "11", "ownerAddress": "0xaaa", "saleStatus": "unlisted", "rarity": "R"},
        ]
    )
    api_client = FakeAPIClient()
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=lambda _: None,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=7):
        result = engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.01,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=False,
        )

    assert result.submitted_count == 1
    active = PositionState(settings.execution_db_path).get_active_offers(chain="bsc")
    assert len(active) == 1
    assert active[0].order_hash == "offer-1"
    assert active[0].collection == TEST_COLLECTION.lower()
    snapshot = PositionState(settings.execution_db_path).get_rate_limit_snapshot(
        now=datetime.now(timezone.utc),
        max_live_offers_per_hour=settings.max_live_offers_per_hour,
        max_bnb_per_day=settings.max_bnb_per_day,
        submit_cooldown_seconds=settings.submit_cooldown_seconds,
    )
    assert snapshot["hourly_count"] == 1


def test_mass_offer_engine_blocks_live_submit_when_governor_disallows(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        buyer_wallet_address=TEST_ACCOUNT.address,
        buyer_wallet_private_key=TEST_PRIVATE_KEY,
        dry_run=False,
        execution_chain="bsc",
        mass_offer_dry_run=False,
        max_live_offers_per_hour=1,
        submit_cooldown_seconds=0,
    )
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    market_client = FakeMarketClient(
        [
            {"tokenId": "21", "ownerAddress": "0xaaa", "saleStatus": "unlisted", "rarity": "R"},
        ]
    )
    api_client = FakeAPIClient()
    state = PositionState(settings.execution_db_path)
    state.record_submit_event(
        engine="counterbid",
        action_type="LIVE_COUNTERBID",
        collection=TEST_COLLECTION,
        chain="bsc",
        price_bnb=0.01,
        status="submitted",
        reason="seed",
    )
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=lambda _: None,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=7):
        result = engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.01,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=False,
        )

    assert result.submitted_count == 0
    assert result.failed_count == 1
    assert api_client.submissions == []
    assert result.results[0].status == "blocked"


def test_mass_offer_cancel_updates_execution_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=False, mass_offer_dry_run=False)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    market_client = FakeMarketClient(
        [
            {"tokenId": "31", "ownerAddress": "0xaaa", "saleStatus": "unlisted", "rarity": "R"},
        ]
    )
    api_client = FakeAPIClient()
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=lambda _: None,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=7):
        engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.01,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=False,
        )

    api_client.my_offers = [
        {
            "offerId": "offer-1",
            "protocolData": {"parameters": {"offerer": TEST_ACCOUNT.address, "zone": "0xzone"}},
        }
    ]
    payload = engine.cancel_active(chain="bsc")

    assert payload["cancelled"] == 1
    assert api_client.cancel_calls == [
        {
            "offer_id": "offer-1",
            "chain": "bsc",
            "order_params": {"offerer": TEST_ACCOUNT.address, "zone": "0xzone"},
        }
    ]
    assert PositionState(settings.execution_db_path).get_active_offers(chain="bsc") == []


def test_mass_offer_status_hides_tracker_records_missing_after_reconcile(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=False, mass_offer_dry_run=False)
    PositionState(settings.execution_db_path).arm_live(minutes=15, actor="test", reason="unit")
    market_client = FakeMarketClient(
        [
            {"tokenId": "41", "ownerAddress": "0xaaa", "saleStatus": "unlisted", "rarity": "R"},
        ]
    )
    api_client = FakeAPIClient()
    engine = MassOfferEngine(
        settings=settings,
        market_client=market_client,
        api_client=api_client,
        offers_provider=FakeOffersProvider(),
        sleep_fn=lambda _: None,
    )

    with patch("okx_nft_bot.mass_offer.engine.get_counter", return_value=7):
        engine.run(
            collection=TEST_COLLECTION,
            chain="bsc",
            price_bnb=0.01,
            rarity_filter=["R"],
            unlisted_only=True,
            dry_run=False,
        )

    state = PositionState(settings.execution_db_path)
    state.mark_offer_status(order_hash="offer-1", status="exchange_missing")
    state.set_runtime_value("last_reconcile_at", "2026-03-29T00:00:00+00:00")

    payload = engine.status(chain="bsc")

    assert payload["active_offer_count"] == 0


def test_mass_offer_cli_commands_use_engine(tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    fake_result = MagicMock()
    fake_result.to_dict.return_value = {"campaign_id": 1, "dry_run": True, "target_count": 2}

    with patch("okx_nft_bot.cli.load_settings", return_value=settings), patch("okx_nft_bot.cli.MassOfferEngine") as mock_engine:
        mock_engine.return_value.run.return_value = fake_result
        assert cmd_mass_offer(
            collection=TEST_COLLECTION,
            chain="bsc",
            price=0.01,
            rarity="R,SR",
            unlisted_only=True,
            include_own=False,
            max_existing_offer=None,
            min_token_id=None,
            max_token_id=None,
            max_offers=5,
            duration_hours=None,
            delay_seconds=None,
            dry_run=True,
        ) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["campaign_id"] == 1
        mock_engine.return_value.run.assert_called_once()

        mock_engine.return_value.status.return_value = {"active_offer_count": 0}
        assert cmd_mass_offer_status(chain="bsc") == 0
        status_payload = json.loads(capsys.readouterr().out)
        assert status_payload["active_offer_count"] == 0

        mock_engine.return_value.cancel_active.return_value = {"cancelled": 2}
        assert cmd_mass_offer_cancel(chain="bsc", collection=None) == 0
        cancel_payload = json.loads(capsys.readouterr().out)
        assert cancel_payload["cancelled"] == 2
