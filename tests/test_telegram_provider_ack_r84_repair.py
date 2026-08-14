from __future__ import annotations

from types import SimpleNamespace

import pytest

import okx_nft_bot.notifiers.telegram as telegram_module
from okx_nft_bot.notifiers.telegram import TelegramNotifier


class _FakeTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0

    def request_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        return dict(self.response)


def _alert(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(telegram_module, "format_text", lambda _alert: "R84 test alert")
    return SimpleNamespace(event=SimpleNamespace(event_id="telegram-r84-event"))


def _notifier(response: dict[str, object]) -> tuple[TelegramNotifier, _FakeTransport]:
    transport = _FakeTransport(response)
    notifier = TelegramNotifier(bot_token="test-token", chat_id="test-chat", transport=transport)  # type: ignore[arg-type]
    return notifier, transport


def test_telegram_provider_rejection_is_not_reported_as_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier, transport = _notifier(
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: simulated provider rejection",
        }
    )

    result = notifier.send(_alert(monkeypatch))

    assert transport.calls == 1
    assert result.delivered is False
    assert result.detail == "telegram_rejected"


def test_telegram_missing_boolean_ok_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier, transport = _notifier({"result": {"message_id": 123}})

    with pytest.raises(RuntimeError, match="boolean 'ok'"):
        notifier.send(_alert(monkeypatch))

    assert transport.calls == 1


def test_telegram_explicit_ok_true_remains_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier, transport = _notifier({"ok": True, "result": {"message_id": 123}})

    result = notifier.send(_alert(monkeypatch))

    assert transport.calls == 1
    assert result.delivered is True
