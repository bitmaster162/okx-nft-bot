from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping
from urllib import parse

from curl_cffi import requests as _curl_requests
from curl_cffi.const import CurlOpt

from okx_nft_bot.logging_utils import log_event


class HTTPStatusError(RuntimeError):
    def __init__(self, status: int, body: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


class RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        self.rate_per_sec = max(rate_per_sec, 0.1)
        self.min_interval = 1.0 / self.rate_per_sec
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


@dataclass(slots=True)
class StdlibHttpTransport:
    timeout: int
    max_retries: int
    rate_limit_per_sec: float
    _rate_limiter: RateLimiter = field(init=False, repr=False)
    _session: _curl_requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rate_limiter = RateLimiter(self.rate_limit_per_sec)
        self._session = _curl_requests.Session(impersonate="chrome")
        self._session.curl.setopt(CurlOpt.IPRESOLVE, 1)  # CURL_IPRESOLVE_V4

    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str = "",
    ) -> dict[str, Any]:
        method = method.upper()
        for attempt in range(1, self.max_retries + 1):
            self._rate_limiter.wait()
            started = time.monotonic()
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    headers=dict(headers),
                    data=body.encode("utf-8") if body else None,
                    timeout=self.timeout,
                )
                latency_ms = round((time.monotonic() - started) * 1000, 2)
                if resp.ok:
                    log_event("http_ok", method=method, url=url, status=resp.status_code, latency_ms=latency_ms)
                    return resp.json()
                body_text = resp.text
                is_retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
                if is_retryable and attempt < self.max_retries:
                    log_event("http_retry", method=method, url=url, status=resp.status_code, attempt=attempt, latency_ms=latency_ms)
                    self._sleep_backoff(attempt, resp.headers)
                    continue
                if not is_retryable or attempt >= self.max_retries:
                    log_event("http_error", method=method, url=url, status=resp.status_code, attempt=attempt, latency_ms=latency_ms, body=body_text[:300])
                raise HTTPStatusError(status=resp.status_code, body=body_text, headers=dict(resp.headers))
            except _curl_requests.errors.RequestsError as exc:
                log_event("http_transport_error", method=method, url=url, attempt=attempt, detail=str(exc))
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt, None)
                    continue
                raise RuntimeError(f"Transport failed after {attempt} attempts: {exc}") from exc
        raise RuntimeError("Unexpected retry loop exit")

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
        if not headers:
            return None
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _sleep_backoff(self, attempt: int, headers: Mapping[str, str] | None) -> None:
        retry_after = self._retry_after_seconds(headers)
        if retry_after is not None:
            time.sleep(max(retry_after, 0.0))
            return
        time.sleep(min(2 ** (attempt - 1), 8.0))


def build_url(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> tuple[str, str]:
    query_pairs = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        query_pairs.append((key, str(value)))
    query = parse.urlencode(query_pairs)
    request_path = path + (("?" + query) if query else "")
    return parse.urljoin(base_url, request_path), request_path
