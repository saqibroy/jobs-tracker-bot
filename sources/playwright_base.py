"""Shared async Playwright context manager for headless Chromium scraping.

Provides ``get_playwright_page()`` — an async context manager that:
  - Launches headless Chromium with a realistic user-agent
  - Sets viewport to 1280×800, locale en-US
  - Blocks images / fonts for faster page loads
  - Handles cleanup (page → context → browser) on exit
  - Default navigation timeout: 30 000 ms

Also provides ``get_shared_browser()`` for reusing a single browser
instance across multiple Playwright sources within one scan cycle.

Usage (standalone):
    async with get_playwright_page() as page:
        await page.goto("https://example.com")
        html = await page.content()

Usage (shared browser, for performance):
    async with shared_browser_context() as browser:
        page = await new_page_from_browser(browser)
        ...
        await page.close()
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger

# Realistic browser fingerprint
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1280, "height": 800}
_LOCALE = "en-US"
_DEFAULT_TIMEOUT = 30_000  # 30 seconds

# Resource types to block (speeds up scraping significantly)
_BLOCKED_RESOURCE_PATTERN = "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot,ico,webp}"


def _playwright_available() -> bool:
    """Check whether Playwright is installed and importable."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except ImportError:
        return False


@asynccontextmanager
async def get_playwright_page(
    *,
    timeout: int = _DEFAULT_TIMEOUT,
) -> AsyncGenerator:
    """Async context manager that yields a ready-to-use Playwright Page.

    Launches a *new* browser, creates one context + page, and tears
    everything down on exit.  Use ``shared_browser_context`` when you
    need to reuse the same browser across several scrapers.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = None
    context = None
    page = None

    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport=_VIEWPORT,
            locale=_LOCALE,
        )
        context.set_default_timeout(timeout)
        context.set_default_navigation_timeout(timeout)

        page = await context.new_page()

        # Block heavy resources to speed up scraping
        await page.route(_BLOCKED_RESOURCE_PATTERN, lambda route: route.abort())

        yield page

    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass


@asynccontextmanager
async def shared_browser_context() -> AsyncGenerator:
    """Async context manager that yields a shared Playwright Browser.

    Caller is responsible for creating pages via ``new_page_from_browser``
    and closing them when done.  The browser itself is closed on exit.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = None

    try:
        browser = await pw.chromium.launch(headless=True)
        yield browser
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass


async def new_page_from_browser(browser, *, timeout: int = _DEFAULT_TIMEOUT):
    """Create a new page from an existing browser with standard settings.

    Returns (context, page).  Caller should close the page and context
    when done.
    """
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=_VIEWPORT,
        locale=_LOCALE,
    )
    context.set_default_timeout(timeout)
    context.set_default_navigation_timeout(timeout)

    page = await context.new_page()
    await page.route(_BLOCKED_RESOURCE_PATTERN, lambda route: route.abort())

    return context, page
