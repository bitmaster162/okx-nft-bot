from __future__ import annotations

from types import SimpleNamespace

import okx_nft_bot.telegram_bot as telegram


def _processor():
    processor = telegram.TelegramCommandProcessor.__new__(telegram.TelegramCommandProcessor)
    processor.settings = SimpleNamespace(execution_db_path="unused")
    return processor


def test_telegram_killswitch_delegates_to_multichain_coordinator(monkeypatch) -> None:
    calls: list[object] = []
    sentinel = object()

    def _activate(*, settings):
        calls.append(settings)
        return sentinel

    monkeypatch.setattr(telegram, "activate_multichain_killswitch", _activate)
    monkeypatch.setattr(telegram, "format_killswitch_result", lambda result: "MULTICHAIN_PASS" if result is sentinel else "bad")

    processor = _processor()
    text = processor._killswitch_command([])

    assert text == "MULTICHAIN_PASS"
    assert calls == [processor.settings]


def test_telegram_killswitch_rejects_arguments_before_coordinator(monkeypatch) -> None:
    def _unexpected(**_kwargs):
        raise AssertionError("coordinator must not run for invalid usage")

    monkeypatch.setattr(telegram, "activate_multichain_killswitch", _unexpected)

    assert _processor()._killswitch_command(["eth"]) == "Usage: /killswitch"
