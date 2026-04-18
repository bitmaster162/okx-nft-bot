from datetime import datetime, timezone

from okx_nft_bot.models import FilterDecision, NFTEvent
from okx_nft_bot.notifiers.base import AlertEnvelope
from okx_nft_bot.notifiers.telegram import TelegramNotifier


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def request_json(self, *, method: str, url: str, headers: dict[str, str], body: str = "") -> dict:
        self.calls.append((method, url, headers, body))
        return {"ok": True, "result": {"message_id": 1}}


def test_telegram_notifier_calls_send_message() -> None:
    transport = FakeTransport()
    notifier = TelegramNotifier(bot_token="token", chat_id="123", transport=transport)
    alert = AlertEnvelope(
        event=NFTEvent(
            event_id="evt-1",
            market="okx",
            event_type="sale",
            collection="Test Collection",
            token_id="1",
            event_time=datetime.now(timezone.utc),
            raw_source="test",
        ),
        decision=FilterDecision(event_id="evt-1", passed=True, matched_rules=["r1"]),
    )

    result = notifier.send(alert)

    assert result.delivered is True
    assert transport.calls
    method, url, _headers, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/sendMessage")
    assert "chat_id=123" in body
