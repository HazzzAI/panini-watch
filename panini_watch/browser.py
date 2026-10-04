"""Polite page fetching through a real (headless) Chrome.

Panini's shops run a JavaScript/cookie gate in front of most requests, so plain HTTP clients get
bounced. A real browser passes it exactly like a normal shopper. We keep the request rate low:
one page at a time per site, with a pause between pages, and we back off (never try to skip) when
a waiting room is shown.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import os
import random
import sys
from typing import Awaitable, Callable

from curl_cffi.requests import AsyncSession

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .models import Listing
from .parsers import parse_listing, with_query

log = logging.getLogger("panini.browser")

_BLOCKED_TYPES = {"image", "media", "font"}
_BLOCKED_HOSTS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook", "hotjar", "newrelic",
    "adobedc", "cookiebot", "clarity.ms", "tiktok", "criteo", "youtube",
)

LISTING_SELECTOR = "div.products li.product-item, .message.notice, .message.info, .page-title"


class WaitingRoom(Exception):
    """The shop is showing a real waiting room / queue. We must wait, not bypass."""


class Fetcher:
    def __init__(self, *, headless: bool = True, page_delay: tuple[float, float] = (1.0, 2.5),
                 max_sites_parallel: int = 3, timeout_s: int = 45):
        self.headless = headless
        self.page_delay = page_delay
        self.timeout_ms = timeout_s * 1000
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._site_locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Semaphore(max_sites_parallel)
        self._start_lock = asyncio.Lock()
        self.proxy = os.getenv("PANINI_PROXY") or None      # optional: http://user:pass@host:port
        self._launch_args = ["--no-sandbox", "--disable-dev-shm-usage"] if sys.platform != "win32" else []
        # fast path: after the browser passes a shop's gate once, plain HTTP reuses its session
        self.http_per_site = 4
        self._http: dict[str, AsyncSession] = {}
        self._http_sem: dict[str, asyncio.Semaphore] = {}
        self._http_global = asyncio.Semaphore(24)
        self._warm_locks: dict[str, asyncio.Lock] = {}
        self._http_broken: dict[str, int] = {}     # consecutive fast-path failures per shop

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser:
                return
            self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.launch(channel="chrome", headless=self.headless,
                                                               args=self._launch_args, proxy=self._proxy_cfg())
            except Exception:  # Chrome not installed -> bundled Chromium
                log.info("Chrome not found, using bundled Chromium")
                self._browser = await self._pw.chromium.launch(headless=self.headless, args=self._launch_args,
                                                               proxy=self._proxy_cfg())

    def _proxy_cfg(self) -> dict | None:
        if not self.proxy:
            return None
        from urllib.parse import urlparse
        u = urlparse(self.proxy)
        cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            cfg.update(username=u.username, password=u.password or "")
        return cfg

    async def close(self) -> None:
        for sess in self._http.values():
            try:
                await sess.close()
            except Exception:
                pass
        self._http.clear()
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def _context(self, site: str, locale: str) -> BrowserContext:
        ctx = self._contexts.get(site)
        if ctx is None:
            await self.start()
            assert self._browser
            ctx = await self._browser.new_context(
                locale=locale, viewport={"width": 1366, "height": 900},
            )

            async def _route(route):
                req = route.request
                if req.resource_type in _BLOCKED_TYPES or any(h in req.url for h in _BLOCKED_HOSTS):
                    await route.abort()
                else:
                    await route.continue_()

            await ctx.route("**/*", _route)
            self._contexts[site] = ctx
        return ctx

    async def with_page(self, site: str, locale: str,
                        fn: Callable[[Page], Awaitable[object]]):
        """Run `fn(page)` for a site, one page at a time per site, a few sites in parallel."""
        lock = self._site_locks.setdefault(site, asyncio.Lock())
        async with lock, self._global:
            ctx = await self._context(site, locale)
            page = await ctx.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                return await fn(page)
            finally:
                await page.close()

    async def _pause(self) -> None:
        await asyncio.sleep(random.uniform(*self.page_delay))

    async def _goto(self, page: Page, url: str, selector: str = LISTING_SELECTOR, timeout_ms: int | None = None) -> str:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms or self.timeout_ms)
        if "queue-it.net" in page.url and "queueittoken" not in page.url:
            # The cookie gate redirects through queue-it for a moment; give it time to pass through.
            try:
                await page.wait_for_url(lambda u: "queue-it.net" not in u, timeout=20000)
            except Exception:
                raise WaitingRoom(page.url)
        try:
            await page.wait_for_selector(selector, timeout=20000, state="attached")
        except Exception:
            pass
        return await page.content()

    async def html(self, site: str, locale: str, url: str, selector: str = LISTING_SELECTOR) -> str:
        async def run(page: Page) -> str:
            html = await self._goto(page, url, selector)
            await self._pause()
            return html
        return await self.with_page(site, locale, run)


    # ------------------------------------------------------------------------------------------
    # fast path (plain HTTP with the browser's session cookies)
    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _gated(status: int, text: str, url: str) -> bool:
        if status in (403, 429, 503) or "queue-it.net" in url:
            return True
        return len(text) < 8000 and ("cookietest" in text or "queue-it" in text.lower())

    async def _warm(self, site: str, locale: str, url: str) -> AsyncSession:
        """Let the real browser pass the shop's gate, then copy its cookies into a fast HTTP session."""
        lock = self._warm_locks.setdefault(site, asyncio.Lock())
        async with lock:
            sess = self._http.get(site)
            if sess is not None and getattr(sess, "_fresh", False):
                return sess
            async def run(page: Page):
                await self._goto(page, url, "body", timeout_ms=90000)
                return await page.context.cookies()
            cookies = await self.with_page(site, locale, run)
            if sess is not None:
                await sess.close()
            sess = AsyncSession(impersonate="chrome", headers={"Accept-Language": f"{locale},en;q=0.8"},
                                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None)
            for c in cookies:
                sess.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
            sess._fresh = True  # type: ignore[attr-defined]
            self._http[site] = sess
            return sess

    async def prewarm(self, site: str, locale: str, url: str) -> None:
        """Pass a shop's gate in the background so the first real request is already fast."""
        try:
            await self._warm(site, locale, url)
        except Exception as e:
            log.debug("[%s] prewarm failed: %s", site, e)

    async def _http_get(self, site: str, locale: str, url: str) -> str | None:
        """GET through the fast session; None when the gate bounced us (caller falls back to the browser)."""
        for attempt in (0, 1):
            try:
                sess = self._http.get(site) or await self._warm(site, locale, url)
            except Exception as e:                   # warm-up page was slow: let the caller fall back
                log.debug("[%s] warm-up failed: %s", site, e)
                self._http_broken[site] = self._http_broken.get(site, 0) + 1
                return None
            sem = self._http_sem.setdefault(site, asyncio.Semaphore(self.http_per_site))
            try:
                async with self._http_global, sem:
                    r = await sess.get(url, timeout=30, allow_redirects=True)
                text = r.text
            except Exception as e:
                log.debug("[%s] http error %s", site, e)
                return None
            if not self._gated(r.status_code, text, str(r.url)):
                self._http_broken[site] = 0
                return text
            if attempt == 0:                       # session expired -> let the browser pass again
                sess._fresh = False                # type: ignore[attr-defined]
                self._http.pop(site, None)
        self._http_broken[site] = self._http_broken.get(site, 0) + 1
        return None

    async def _fast_listing(self, site: str, locale: str, url: str, max_pages: int, per_page: int) -> Listing | None:
        if self._http_broken.get(site, 0) >= 3:
            return None
        first = await self._http_get(site, locale, with_query(url, product_list_limit=per_page))
        if first is None or ("mage/" not in first and "Magento_" not in first):
            return None
        products, total, nxt = parse_listing(first, site, url)
        seen = {p.pid for p in products}
        if total and total > len(products):
            pages = min(max_pages, math.ceil(total / per_page))
            htmls = await asyncio.gather(*[
                self._http_get(site, locale, with_query(url, product_list_limit=per_page, p=n))
                for n in range(2, pages + 1)])
            if any(h is None for h in htmls):
                return None
            for h in htmls:
                for p in parse_listing(h, site, url)[0]:
                    if p.pid not in seen:
                        seen.add(p.pid)
                        products.append(p)
        elif nxt:                                   # total unknown: follow "next" links
            n = 1
            while nxt and n < max_pages:
                h = await self._http_get(site, locale, nxt)
                if h is None:
                    return None
                got, _, nxt = parse_listing(h, site, nxt)
                fresh = [p for p in got if p.pid not in seen]
                if not fresh:
                    break
                for p in fresh:
                    seen.add(p.pid)
                    products.append(p)
                n += 1
        complete = total is None or len(products) >= total or (total and total > max_pages * per_page and False)
        if total and len(products) < total:
            complete = False
        return Listing(url, products, total, complete, "")

    async def json(self, site: str, locale: str, url: str):
        """GET a JSON endpoint (Shopify storefronts) through the same browser session."""
        if self._http_broken.get(site, 0) < 3:
            text = await self._http_get(site, locale, url)
            if text and text.lstrip()[:1] in "{[":
                try:
                    return _json.loads(text)
                except ValueError:
                    pass

        async def run(page: Page):
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if resp is None or resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status if resp else '?'} for {url}")
            body = await page.inner_text("body")
            await self._pause()
            return _json.loads(body)
        return await self.with_page(site, locale, run)

    async def listing(self, site: str, locale: str, url: str, *, max_pages: int = 8,
                      per_page: int = 36) -> Listing:
        """Read a category or search result, following pagination."""
        fast = await self._fast_listing(site, locale, url, max_pages, per_page)
        if fast is not None:
            return fast
        async def run(page: Page) -> Listing:
            products, seen, total = [], set(), None
            nxt: str | None = with_query(url, product_list_limit=per_page)
            pages = 0
            complete = True
            err = ""
            while nxt and pages < max_pages:
                try:
                    html = await self._goto(page, nxt)
                except WaitingRoom as e:
                    return Listing(url, products, total, False, f"waiting room: {e}")
                except Exception as e:  # network/timeout
                    complete, err = False, str(e)[:200]
                    break
                got, t, nxt_url = parse_listing(html, site, page.url)
                total = t if t is not None else total
                fresh = [p for p in got if p.pid not in seen]
                for p in fresh:
                    seen.add(p.pid)
                    products.append(p)
                pages += 1
                if not fresh:
                    break
                nxt = nxt_url
                if nxt:
                    await self._pause()
            if nxt and pages >= max_pages:
                complete = False
            if total is not None and len(products) < total and complete:
                complete = False
            return Listing(url, products, total, complete, err)
        return await self.with_page(site, locale, run)
