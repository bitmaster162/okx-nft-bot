"""
Playwright-based OKX global sales stream — ALL chains via multi-page.

Strategy:
1. Launch headless Chromium with OKX cookies
2. Open Activity page → OKX JS natively polls collectionHistory (default=ETH)
3. Open additional pages, click chain filter to select BSC/Polygon/Arbitrum
4. Intercept ALL collectionHistory responses across all pages
5. Scroll all pages to keep native polling alive
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("sales_stream.playwright")

PRIAPI_PATTERN = "/priapi/v1/nft/trading/collectionHistory"
ACTIVITY_URL = "https://web3.okx.com/ru/nft/activity"
COOKIES_PATH = "config/okx_cookies.json"

CHAIN_ID_TO_NAME: dict[str, str] = {
    "1": "eth", "56": "bsc", "137": "polygon",
    "42161": "arbitrum", "10": "optimism", "43114": "avalanche",
    "324": "zksync", "8453": "base", "59144": "linea",
}

# Chains to open separate pages for (besides ETH which comes from main page).
# Key = page name, value = chain label text to click in the filter dropdown (Russian UI).
OTHER_CHAIN_PAGES: dict[str, list[str]] = {
    "bsc": ["BSC", "BNB Chain", "BNB Smart Chain", "BNB"],
    "polygon": ["Polygon"],
    "arbitrum": ["Arbitrum", "Arbitrum One"],
}


class OKXGlobalStream:
    """Multi-page Activity intercept — one page per chain."""

    def __init__(
        self,
        scroll_interval: float = 6.0,
        headless: bool = True,
        cookies_path: str | None = None,
    ):
        self.scroll_interval = scroll_interval
        self.headless = headless
        self.cookies_path = cookies_path or COOKIES_PATH

        self._browser = None
        self._context = None
        self._pages: dict[str, any] = {}
        self._playwright = None
        self._trade_buffer: list[dict] = []
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._total_intercepted = 0
        self._by_chain: dict[str, int] = {}
        self._seen_ids: set[str] = set()
        self._max_seen = 50_000

    # ─── Cookie loading ──────────────────────────────────────────

    async def _load_cookies(self) -> int:
        import pathlib
        cookie_file = pathlib.Path(self.cookies_path)
        if not cookie_file.exists():
            log.warning("Cookie file not found: %s", self.cookies_path)
            return 0
        try:
            raw = cookie_file.read_text(encoding="utf-8")
            cookies_list = json.loads(raw)
            if not isinstance(cookies_list, list):
                return 0
            pw_cookies = []
            for c in cookies_list:
                entry = {
                    "name": str(c.get("name", "")),
                    "value": str(c.get("value", "")),
                    "domain": str(c.get("domain", ".okx.com")),
                    "path": str(c.get("path", "/")),
                }
                if c.get("httpOnly"):
                    entry["httpOnly"] = True
                if c.get("secure"):
                    entry["secure"] = True
                if entry["name"] and entry["value"]:
                    pw_cookies.append(entry)
            if pw_cookies:
                await self._context.add_cookies(pw_cookies)
                log.info("Loaded %d cookies", len(pw_cookies))
            return len(pw_cookies)
        except Exception as exc:
            log.error("Cookie load error: %s", exc)
            return 0

    # ─── Response interception ───────────────────────────────────

    async def _on_response(self, response):
        """Intercept collectionHistory from ANY page."""
        url = response.url
        if PRIAPI_PATTERN not in url:
            return
        try:
            body = await response.json()
            if str(body.get("code", -1)) != "0":
                return
            n = self._parse_and_buffer(body, url)
            if n > 0:
                params = parse_qs(urlparse(url).query)
                chain_id = (params.get("chain") or ["?"])[0]
                chain = CHAIN_ID_TO_NAME.get(chain_id, chain_id)
                log.info("[%s] +%d trades (buffer=%d, total=%d)",
                         chain.upper(), n, len(self._trade_buffer), self._total_intercepted)
        except Exception:
            pass

    def _parse_and_buffer(self, body: dict, url: str = "") -> int:
        try:
            chain_id = ""
            if url:
                params = parse_qs(urlparse(url).query)
                chain_id = (params.get("chain") or [""])[0]
            chain_name = CHAIN_ID_TO_NAME.get(chain_id, f"chain_{chain_id}")

            data = body.get("data", {})
            if isinstance(data, list):
                trades = data
            elif isinstance(data, dict):
                trades = data.get("data", data.get("list", data.get("items", [])))
            else:
                return 0
            if not trades:
                return 0

            new_trades = []
            for t in trades:
                tid = t.get("orderId") or t.get("id") or t.get("txHash", "") + str(t.get("tokenId", ""))
                if tid and tid in self._seen_ids:
                    continue
                if tid:
                    self._seen_ids.add(tid)
                t["_pw_chain"] = chain_name
                t["_pw_chain_id"] = chain_id
                new_trades.append(t)

            if new_trades:
                self._trade_buffer.extend(new_trades)
                self._total_intercepted += len(new_trades)
                self._by_chain[chain_name] = self._by_chain.get(chain_name, 0) + len(new_trades)

            if len(self._seen_ids) > self._max_seen:
                excess = len(self._seen_ids) - self._max_seen // 2
                it = iter(self._seen_ids)
                for _ in range(excess):
                    self._seen_ids.discard(next(it))
            return len(new_trades)
        except Exception:
            return 0

    # ─── Chain filter selection ──────────────────────────────────

    async def _select_chain_on_page(self, page, chain_name: str, chain_labels: list[str]) -> bool:
        """Click the network filter on the Activity page and select a chain.

        The filter button shows 'Сеть: Ethereum (N)' — click it to open dropdown,
        then click the desired chain name.
        """
        try:
            # Step 1: Find and click the "Сеть:" filter button to open dropdown
            filter_selectors = [
                # Russian UI
                "button:has-text('Сеть:')",
                "div:has-text('Сеть:') >> button",
                "span:has-text('Сеть:')",
                # English UI
                "button:has-text('Network:')",
                "span:has-text('Network:')",
                # Generic: look for filter with Ethereum text
                "button:has-text('Ethereum')",
                "*:has-text('Сеть') >> nth=0",
            ]

            clicked_filter = False
            for sel in filter_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        await el.click()
                        clicked_filter = True
                        log.info("%s: Clicked filter: %s", chain_name, sel)
                        await asyncio.sleep(1)
                        break
                except Exception:
                    continue

            if not clicked_filter:
                log.warning("%s: Could not find network filter button", chain_name)
                # Try screenshot for debug
                try:
                    await page.screenshot(path=f"/app/data/okx_debug_{chain_name}.png")
                except Exception:
                    pass
                return False

            # Step 2: Click desired chain in dropdown
            for label in chain_labels:
                try:
                    # Look in dropdown/popover for chain name
                    chain_el = page.locator(
                        f"div[role='listbox'] >> text='{label}', "
                        f"div[class*='dropdown'] >> text='{label}', "
                        f"div[class*='popover'] >> text='{label}', "
                        f"div[class*='menu'] >> text='{label}', "
                        f"li:has-text('{label}'), "
                        f"div[class*='option']:has-text('{label}'), "
                        f"span:has-text('{label}')"
                    ).first
                    if await chain_el.is_visible(timeout=3000):
                        await chain_el.click()
                        log.info("%s: Selected chain '%s' in dropdown", chain_name, label)
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    continue

            # Step 3: Fallback — try direct text click anywhere visible
            for label in chain_labels:
                try:
                    el = page.get_by_text(label, exact=True).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        log.info("%s: Clicked chain text '%s'", chain_name, label)
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    continue

            log.warning("%s: Could not select chain in dropdown (tried: %s)",
                        chain_name, chain_labels)
            try:
                await page.screenshot(path=f"/app/data/okx_debug_{chain_name}_dropdown.png")
            except Exception:
                pass
            return False

        except Exception as exc:
            log.warning("%s: Chain selection error: %s", chain_name, exc)
            return False

    # ─── Start ───────────────────────────────────────────────────

    async def start(self):
        from playwright.async_api import async_playwright

        log.info("Starting OKX global stream (multi-page, all chains)...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions", "--no-first-run",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )

        n_cookies = await self._load_cookies()

        # ── Main page (ETH) ──
        main_page = await self._context.new_page()
        main_page.on("response", self._on_response)
        self._pages["eth"] = main_page

        log.info("Loading main Activity page (ETH default)...")
        try:
            await main_page.goto(ACTIVITY_URL, wait_until="load", timeout=60_000)
        except Exception as exc:
            log.warning("Main page load: %s", exc)

        log.info("Waiting 25s for main page SPA init...")
        await asyncio.sleep(25)
        log.info("Main page init: buffer=%d", len(self._trade_buffer))

        # Screenshot main page
        try:
            await main_page.screenshot(path="/app/data/okx_debug_screenshot.png")
        except Exception:
            pass

        # ── Additional pages for other chains ──
        for chain_name, chain_labels in OTHER_CHAIN_PAGES.items():
            try:
                page = await self._context.new_page()
                page.on("response", self._on_response)
                self._pages[chain_name] = page

                log.info("Loading %s page...", chain_name.upper())
                await page.goto(ACTIVITY_URL, wait_until="load", timeout=60_000)
                await asyncio.sleep(15)  # Let SPA init

                # Select chain filter
                ok = await self._select_chain_on_page(page, chain_name, chain_labels)
                if ok:
                    # Wait for chain-specific data to flow
                    await asyncio.sleep(10)
                    log.info("%s: page ready, buffer=%d chains=%s",
                             chain_name.upper(), len(self._trade_buffer), dict(self._by_chain))
                else:
                    log.warning("%s: chain filter selection failed — page may still show ETH",
                                chain_name.upper())

            except Exception as exc:
                log.warning("Failed to setup %s page: %s", chain_name, exc)

        log.info("All pages ready: buffer=%d total=%d chains=%s pages=%d",
                 len(self._trade_buffer), self._total_intercepted,
                 dict(self._by_chain), len(self._pages))

        self._running = True
        self._loop_task = asyncio.create_task(self._main_loop())
        log.info("OKXGlobalStream started (%d pages)", len(self._pages))

    # ─── Main loop ───────────────────────────────────────────────

    async def _main_loop(self):
        tick = 0
        last_data_time = time.monotonic()
        last_total = 0

        while self._running:
            try:
                await asyncio.sleep(self.scroll_interval)
                tick += 1

                if self._total_intercepted > last_total:
                    last_data_time = time.monotonic()
                    last_total = self._total_intercepted

                # Scroll all pages
                for name, page in list(self._pages.items()):
                    if page.is_closed():
                        continue
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(0.2)
                        await page.evaluate("window.scrollTo(0, 0)")
                    except Exception:
                        pass

                if tick % 10 == 0:
                    active = sum(1 for p in self._pages.values() if not p.is_closed())
                    log.info("tick=%d buffer=%d total=%d chains=%s pages=%d/%d",
                             tick, len(self._trade_buffer), self._total_intercepted,
                             dict(self._by_chain), active, len(self._pages))

                # Reload if no data for 3 min
                now = time.monotonic()
                if now - last_data_time > 180:
                    log.warning("No data for 180s — reloading all pages...")
                    for name, page in list(self._pages.items()):
                        if page.is_closed():
                            continue
                        try:
                            await page.reload(wait_until="load", timeout=60_000)
                        except Exception:
                            pass
                    await asyncio.sleep(20)
                    last_data_time = time.monotonic()

            except Exception as exc:
                if self._running:
                    log.debug("Loop error: %s", exc)

    # ─── Public API ──────────────────────────────────────────────

    def drain_trades(self) -> list[dict]:
        trades = self._trade_buffer[:]
        self._trade_buffer.clear()
        return trades

    def get_stats(self) -> dict:
        return {
            "total_intercepted": self._total_intercepted,
            "buffer_size": len(self._trade_buffer),
            "by_chain": dict(self._by_chain),
            "pages": len(self._pages),
        }

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for page in self._pages.values():
            try:
                await page.close()
            except Exception:
                pass
        for obj in (self._context, self._browser):
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        log.info("OKXGlobalStream stopped (total=%d, chains=%s)",
                 self._total_intercepted, self._by_chain)

    @property
    def is_running(self) -> bool:
        return self._running and self._browser is not None


# ─── Sync wrapper ────────────────────────────────────────────────

class SyncPlaywrightOKXStream:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._stream: OKXGlobalStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = None

    def start(self):
        import threading
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="playwright-okx"
        )
        self._thread.start()
        deadline = time.monotonic() + 240  # 4 min for multi-page init
        while time.monotonic() < deadline:
            if self._stream and self._stream.is_running:
                log.info("SyncPlaywrightOKXStream ready")
                return
            time.sleep(1)
        log.error("SyncPlaywrightOKXStream failed to start within 240s")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        async def _main():
            self._stream = OKXGlobalStream(**self._kwargs)
            await self._stream.start()
            while self._stream.is_running:
                await asyncio.sleep(1)
        try:
            self._loop.run_until_complete(_main())
        except Exception as exc:
            log.error("Playwright event loop crashed: %s", exc)
        finally:
            self._loop.close()

    def drain_trades(self) -> list[dict]:
        return self._stream.drain_trades() if self._stream else []

    def stop(self):
        if self._stream and self._loop:
            f = asyncio.run_coroutine_threadsafe(self._stream.stop(), self._loop)
            try:
                f.result(timeout=15)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._stream is not None and self._stream.is_running
