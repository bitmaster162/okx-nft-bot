from __future__ import annotations

from dataclasses import dataclass, field
import threading
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
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


# Shared global OKX rate limiter — all OKX clients share one limit
_okx_global_limiter: RateLimiter | None = None
_okx_global_lock = threading.Lock()


def get_okx_global_limiter(rate_per_sec: float) -> RateLimiter:
    """Get or create the shared OKX rate limiter (uses slowest rate)."""
    global _okx_global_limiter
    with _okx_global_lock:
        if _okx_global_limiter is None:
            _okx_global_limiter = RateLimiter(rate_per_sec)
        elif rate_per_sec < _okx_global_limiter.rate_per_sec:
            _okx_global_limiter = RateLimiter(rate_per_sec)
    return _okx_global_limiter


# Shared global RPC rate limiter — all JSON-RPC callers share one limit
# so public endpoints (bsc-dataseed, cloudflare-eth) don't 429 under load.
_rpc_global_limiter: RateLimiter | None = None
_rpc_global_lock = threading.Lock()


def get_rpc_global_limiter(rate_per_sec: float = 10.0) -> RateLimiter:
    """Get or create the shared RPC rate limiter (uses slowest rate)."""
    global _rpc_global_limiter
    with _rpc_global_lock:
        if _rpc_global_limiter is None:
            _rpc_global_limiter = RateLimiter(rate_per_sec)
        elif rate_per_sec < _rpc_global_limiter.rate_per_sec:
            _rpc_global_limiter = RateLimiter(rate_per_sec)
    return _rpc_global_limiter


# Shared global RPC transport — POST JSON-RPC through curl_cffi with retries
_rpc_transport: StdlibHttpTransport | None = None
_rpc_transport_lock = threading.Lock()


def get_rpc_transport(timeout: int = 5, max_retries: int = 2, rate_per_sec: float = 10.0) -> StdlibHttpTransport:
    """Get the shared RPC HTTP transport (hooks into the shared RPC limiter)."""
    global _rpc_transport
    with _rpc_transport_lock:
        if _rpc_transport is None:
            t = StdlibHttpTransport(timeout=timeout, max_retries=max_retries, rate_limit_per_sec=rate_per_sec)
            t._rate_limiter = get_rpc_global_limiter(rate_per_sec)
            _rpc_transport = t
    return _rpc_transport


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
                log_event("http_error", method=method, url=url, status=resp.status_code, attempt=attempt, latency_ms=latency_ms, body=body_text[:300])
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt, resp.headers)
                        continue
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
