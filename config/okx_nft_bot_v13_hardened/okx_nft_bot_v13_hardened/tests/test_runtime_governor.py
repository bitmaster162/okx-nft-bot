from __future__ import annotations

from pathlib import Path
import sqlite3

from okx_nft_bot.sniper.buyer import OKXInstantBuyer
from okx_nft_bot.sniper.offer_blaster import OfferBlaster
from okx_nft_bot.undercutter.state import PositionState


def test_instant_buyer_forces_dry_run_when_execution_killswitch_is_on(tmp_path: Path, monkeypatch) -> None:
    execution_db_path = tmp_path / "execution.sqlite3"
    PositionState(execution_db_path).set_force_dry_run(True, reason="test")

    monkeypatch.setenv("AUTO_BUY_ENABLED", "1")
    monkeypatch.setenv("AUTO_BUY_DRY_RUN", "0")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("EXECUTION_DB_PATH", str(execution_db_path))
    monkeypatch.setattr(OKXInstantBuyer, "_init_web3", lambda self: None)

    buyer = OKXInstantBuyer()

    assert buyer._effective_dry_run() is True


def test_offer_blaster_uses_effective_dry_run_for_bsc_blasts(tmp_path: Path, monkeypatch) -> None:
    execution_db_path = tmp_path / "execution.sqlite3"
    PositionState(execution_db_path).set_force_dry_run(True, reason="test")

    monkeypatch.setenv("OFFER_BLASTER_ENABLED", "1")
    monkeypatch.setenv("OFFER_BLASTER_DRY_RUN", "0")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("EXECUTION_DB_PATH", str(execution_db_path))

    captured: dict[str, object] = {}

    class FakeEngine:
        def run(self, **kwargs):
            captured.update(kwargs)

            class Result:
                submitted = 0
                failed = 0
                skipped = 0

            return Result()

    blaster = OfferBlaster()
    monkeypatch.setattr(blaster, "_get_engine", lambda: FakeEngine())

    result = blaster.blast_collection(
        collection_address="0xabc",
        collection_name="Alpha",
        chain="bsc",
        max_offer_price=0.1,
        currency="WBNB",
    )

    assert result is not None
    assert result.dry_run is True
    assert captured["dry_run"] is True


def test_hidden_execution_helpers_fail_closed_on_invalid_force_dry_run(tmp_path: Path, monkeypatch) -> None:
    execution_db_path = tmp_path / "execution.sqlite3"
    PositionState(execution_db_path)
    with sqlite3.connect(execution_db_path) as conn:
        conn.execute(
            """
            INSERT INTO execution_runtime_state (key, value, updated_at)
            VALUES ('force_dry_run', 'maybe', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """
        )

    monkeypatch.setenv("AUTO_BUY_ENABLED", "1")
    monkeypatch.setenv("AUTO_BUY_DRY_RUN", "0")
    monkeypatch.setenv("OFFER_BLASTER_ENABLED", "1")
    monkeypatch.setenv("OFFER_BLASTER_DRY_RUN", "0")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("EXECUTION_DB_PATH", str(execution_db_path))
    monkeypatch.setattr(OKXInstantBuyer, "_init_web3", lambda self: None)

    buyer = OKXInstantBuyer()
    blaster = OfferBlaster()

    assert buyer._effective_dry_run() is True
    assert blaster._effective_dry_run() is True
