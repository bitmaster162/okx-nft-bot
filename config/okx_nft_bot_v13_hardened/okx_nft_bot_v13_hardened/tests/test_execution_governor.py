from __future__ import annotations

from pathlib import Path
import sqlite3

from okx_nft_bot.config import Settings
from okx_nft_bot.execution_governor import ExecutionGovernor
from okx_nft_bot.undercutter.state import PositionState


class FakeAPIClient:
    def __init__(self, offers: list[dict[str, object]]) -> None:
        self.offers = offers
        self.calls: list[str] = []

    def get_my_offers(self, chain: str = "bsc", **_: object) -> list[dict[str, object]]:
        self.calls.append(chain)
        return list(self.offers)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
        buyer_wallet_address="0xbuyer",
    )


def test_reconcile_active_offers_marks_missing_and_imports_exchange_truth(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="shared", collection="0xshared", chain="bsc", price_bnb=0.33)
    state.upsert_active_offer(order_hash="local-only", collection="0xlocal", chain="bsc", price_bnb=0.50)
    state.upsert_active_offer(order_hash="dryrun-1", collection="0xdry", chain="bsc", price_bnb=0.20)

    governor = ExecutionGovernor(
        settings=settings,
        state=state,
        api_client=FakeAPIClient(
            [
                {"offerId": "shared", "contractAddress": "0xshared", "price": "0.44"},
                {"offerId": "exchange-only", "contractAddress": "0xexchange", "price": "0.55"},
            ]
        ),
    )

    result = governor.reconcile_active_offers(chain="bsc")

    assert result.exchange_seen == 2
    assert result.local_active_seen == 3
    assert result.local_dry_run_ignored == 1
    assert result.local_marked_missing == 1
    assert result.local_refreshed == 1
    assert result.local_added_from_exchange == 1
    assert result.exchange_missing_order_hashes == ["local-only"]
    assert result.imported_order_hashes == ["exchange-only"]

    active = {offer.order_hash: offer for offer in state.get_active_offers(chain="bsc")}
    assert set(active) == {"shared", "exchange-only", "dryrun-1"}
    assert active["shared"].price_bnb == 0.44
    assert active["exchange-only"].collection == "0xexchange"
    runtime = state.get_runtime_state()
    assert runtime["last_reconcile_chain"] == "bsc"
    assert runtime["last_reconcile_local_added"] == "1"


def test_live_submit_requires_active_arm_window(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    governor = ExecutionGovernor(settings=settings, state=state, api_client=FakeAPIClient([]))

    assert governor.check_live_submit_allowed(
        action_type="LIVE_COUNTERBID",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.1,
        configured_dry_run=False,
    ) == "live arm required"

    state.arm_live(minutes=15, actor="test", reason="unit")

    assert governor.check_live_submit_allowed(
        action_type="LIVE_COUNTERBID",
        collection="0xabc",
        chain="bsc",
        price_bnb=0.1,
        configured_dry_run=False,
    ) is None


def test_governor_init_audits_invalid_runtime_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    with sqlite3.connect(settings.execution_db_path) as conn:
        conn.execute(
            """
            INSERT INTO execution_runtime_state (key, value, updated_at)
            VALUES ('force_dry_run', 'maybe', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """
        )

    governor = ExecutionGovernor(settings=settings, state=state, api_client=FakeAPIClient([]))

    assert governor.effective_dry_run(False) is True
    runtime = state.get_runtime_state()
    assert runtime["force_dry_run"] == "1"


def test_reconcile_active_offers_raises_on_partial_exchange_failure(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = PositionState(settings.execution_db_path)
    state.upsert_active_offer(order_hash="shared", collection="0xshared", chain="bsc", price_bnb=0.33)

    class PartialFailureAPI:
        def get_my_offers(self, chain: str = "bsc", **_: object) -> list[dict[str, object]]:
            raise RuntimeError("get_my_offers: partial endpoint failure — collection-offers: 503")

    governor = ExecutionGovernor(settings=settings, state=state, api_client=PartialFailureAPI())

    try:
        governor.reconcile_active_offers(chain="bsc")
    except RuntimeError as exc:
        assert "partial endpoint failure" in str(exc)
    else:
        raise AssertionError("Expected reconcile_active_offers() to fail on partial endpoint failure")
