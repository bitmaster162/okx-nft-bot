from __future__ import annotations

from pathlib import Path

from okx_nft_bot.config import Settings
from okx_nft_bot.notifiers.factory import build_notifier
from okx_nft_bot.notifiers.fanout import FanoutNotifier
from okx_nft_bot.notifiers.telegram import TelegramNotifier
from okx_nft_bot.notifiers.webhook import WebhookNotifier


def _settings(
    tmp_path: Path,
    *,
    telegram: bool = False,
    webhook: bool = False,
) -> Settings:
    return Settings(
        app_env="test",
        db_path=tmp_path / "state.sqlite3",
        okx_max_retries=4,
        telegram_bot_token="r85-test-token" if telegram else None,
        telegram_chat_id="123456" if telegram else None,
        webhook_url="https://example.invalid/hook" if webhook else None,
    )


def test_telegram_notifier_transport_is_single_attempt(tmp_path: Path) -> None:
    notifier = build_notifier(_settings(tmp_path, telegram=True))

    assert isinstance(notifier, TelegramNotifier)
    assert notifier.transport.max_retries == 1


def test_webhook_notifier_transport_is_single_attempt(tmp_path: Path) -> None:
    notifier = build_notifier(_settings(tmp_path, webhook=True))

    assert isinstance(notifier, WebhookNotifier)
    assert notifier.transport.max_retries == 1


def test_fanout_children_share_single_attempt_transport(tmp_path: Path) -> None:
    notifier = build_notifier(_settings(tmp_path, telegram=True, webhook=True))

    assert isinstance(notifier, FanoutNotifier)
    assert len(notifier.notifiers) == 2
    transports = [child.transport for child in notifier.notifiers]
    assert transports[0] is transports[1]
    assert all(transport.max_retries == 1 for transport in transports)
