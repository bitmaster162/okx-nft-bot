from __future__ import annotations

from types import SimpleNamespace

from okx_nft_bot.clients import http


def test_redact_sensitive_url_masks_telegram_bot_token() -> None:
    token = "123456789:AAExampleSecretToken"
    url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id=42"

    redacted = http._redact_sensitive_url(url)

    assert token not in redacted
    assert redacted == "https://api.telegram.org/bot<redacted>/sendMessage?chat_id=42"


def test_redact_sensitive_url_leaves_non_telegram_url_unchanged() -> None:
    url = "https://web3.okx.com/api/v1/example?cursor=abc"

    assert http._redact_sensitive_url(url) == url


def test_http_transport_logs_redacted_url_but_requests_original(monkeypatch) -> None:
    token = "987654321:AAAnotherSecretToken"
    original_url = f"https://api.telegram.org/bot{token}/getUpdates"
    requested: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []

    class _Response:
        ok = True
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True}

    class _Session:
        @staticmethod
        def request(**kwargs):
            requested.append(str(kwargs["url"]))
            return _Response()

    transport = http.StdlibHttpTransport.__new__(http.StdlibHttpTransport)
    transport.timeout = 1
    transport.max_retries = 1
    transport.rate_limit_per_sec = 100.0
    transport._rate_limiter = SimpleNamespace(wait=lambda: None)
    transport._session = _Session()
    monkeypatch.setattr(http, "log_event", lambda event, **fields: events.append((event, fields)))

    payload = transport.request_json(
        method="POST",
        url=original_url,
        headers={"Accept": "application/json"},
    )

    assert payload == {"ok": True}
    assert requested == [original_url]
    assert len(events) == 1
    assert events[0][0] == "http_ok"
    logged_url = str(events[0][1]["url"])
    assert token not in logged_url
    assert logged_url == "https://api.telegram.org/bot<redacted>/getUpdates"
