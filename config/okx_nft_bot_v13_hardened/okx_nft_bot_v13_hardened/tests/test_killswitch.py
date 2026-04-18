from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from okx_nft_bot.config import Settings
from okx_nft_bot.telegram_bot import TelegramCommandProcessor
from okx_nft_bot.undercutter.state import PositionState


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "db.sqlite3",
        offers_db_path=tmp_path / "offers.sqlite3",
        execution_db_path=tmp_path / "execution.sqlite3",
        execution_chain="bsc",
        dry_run=False,
    )


def _processor(tmp_path: Path) -> TelegramCommandProcessor:
    settings = _settings(tmp_path)
    return TelegramCommandProcessor(
        settings=settings,
        store=MagicMock(),
        registry=MagicMock(),
        runner=MagicMock(),
        client=MagicMock(),
    )


def test_killswitch_marks_failed_live_offer_as_killswitch_failed(tmp_path: Path, monkeypatch) -> None:
    processor = _processor(tmp_path)
    state = PositionState(Path(processor.settings.execution_db_path))
    state.upsert_active_offer(order_hash="live-offer-1", collection="0xabc", chain="bsc", price_bnb=0.5)

    def _cancel(self, offer_id: str) -> bool:
        _ = self, offer_id
        raise RuntimeError("api down")

    monkeypatch.setattr("okx_nft_bot.telegram_bot.OKXAPIClient.cancel_offer", _cancel)

    text = processor._killswitch_command([])

    assert "failed=1" in text
    assert "zombies=1" in text
    refreshed = PositionState(Path(processor.settings.execution_db_path))
    assert refreshed.get_active_offers(chain="bsc") == []
    failed = refreshed.get_killswitch_failed_offers(chain="bsc")
    assert len(failed) == 1
    assert failed[0].order_hash == "live-offer-1"
    assert failed[0].status == "killswitch_failed"
    assert refreshed.is_force_dry_run() is True
