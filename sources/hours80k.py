"""80,000 Hours Job Board — Playwright-based scraper.

URL: https://jobs.80000hours.org/

The job board is a JavaScript-rendered SPA (Next.js). No RSS feed or
public JSON API is available.  We use Playwright to load the page,
wait for job cards to render, and extract listing data.

All 80,000 Hours jobs are from the Effective Altruism / impact sector,
so ``is_ngo=True`` for all listings.

When location is unclear, we default ``remote_scope="worldwide"``
because 80k Hours jobs are often worldwide remote or EU-accessible.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://jobs.80000hours.org"
_SEARCH_URL = (
    f"{_BASE_URL}/"
    "?refinementList%5Bskills%5D%5B0%5D=Software%20engineering"
)

# Timeout for waiting for JS-rendered content
_PAGE_LOAD_TIMEOUT = 15_000  # 15 seconds
_SOURCE_TIMEOUT = 45  # seconds — max time for this entire source

# Playwright is optional — flag tracks availability
_PLAYWRIGHT_AVAILABLE: bool | None = None


def _check_playwright() -> bool:
    """Lazily check whether Playwright is importable."""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            from playwright.async_api import async_playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


class Hours80kSource(BaseSource):
    """80,000 Hours job board scraper using Playwright."""

    name = "hours80k"

    # Optional: a shared browser instance passed from main.py
    _shared_browser = None

    def set_shared_browser(self, browser) -> None:
        """Set a shared Playwright browser instance for reuse."""
        self._shared_browser = browser

    async def fetch(self) -> list[Job]:
        if not _check_playwright():
            logger.warning(
                "[{}] Playwright not installed — skipping source", self.name
            )
            return []

        try:
            return await asyncio.wait_for(
                self._fetch_with_playwright(),
                timeout=_SOURCE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[{}] Timed out after {}s — aborting", self.name, _SOURCE_TIMEOUT
            )
            return []
        except Exception as exc:
            logger.error("[{}] Playwright fetch failed: {}", self.name, exc)
            return []

    async def _fetch_with_playwright(self) -> list[Job]:
        """Launch Playwright, navigate to the page, and extract jobs."""
        if self._shared_browser:
            return await self._scrape_with_browser(self._shared_browser)

        # Standalone mode — create our own browser
        from sources.playwright_base import get_playwright_page

        async with get_playwright_page() as page:
            return await self._scrape_page(page)

    async def _scrape_with_browser(self, browser) -> list[Job]:
        """Use a shared browser instance to scrape."""
        from sources.playwright_base import new_page_from_browser

        context, page = await new_page_from_browser(browser)
        try:
            return await self._scrape_page(page)
        finally:
            await page.close()
            await context.close()

    async def _scrape_page(self, page) -> list[Job]:
        """Navigate to 80k Hours and extract job cards."""
        logger.debug("[{}] Navigating to {}", self.name, _SEARCH_URL)

        await page.goto(_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        # Give JS frameworks time to render
        await page.wait_for_timeout(3000)

        # Wait for job cards to appear — try multiple selectors
        selectors_to_try = [
            ".job-card",
            "[data-testid='job-card']",
            "[class*='JobCard']",
            "[class*='job-card']",
            "article",
            "a[href*='/job/']",
            "li a[href*='/job']",
        ]

        cards = []
        for selector in selectors_to_try:
            try:
                await page.wait_for_selector(selector, timeout=5000)
                cards = await page.query_selector_all(selector)
                if cards:
                    logger.debug(
                        "[{}] Found {} cards with selector '{}'",
                        self.name, len(cards), selector,
                    )
                    break
            except Exception:
                continue

        if not cards:
            # Fallback: extract from full page HTML
            logger.debug("[{}] No card selectors matched — trying HTML fallback", self.name)
            html = await page.content()
            return self._parse_html_fallback(html)

        jobs: list[Job] = []
        for card in cards:
            try:
                job = await self._parse_card_element(card)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug("[{}] Skipping card: {}", self.name, exc)

        logger.info("[{}] Extracted {} jobs from page", self.name, len(jobs))
        return jobs

    async def _parse_card_element(self, card) -> Job | None:
        """Extract job data from a Playwright element handle.

        Actual card structure (as of 2025):
          <div class="job-card">
            <a href="https://app.80000hours.org/job/conversation/?jobId=XXXXX">
              <p class="font-bold"><span>Job Title</span></p>
              ...text lines:
                [optional "Highlighted role"]
                Company Name
                Location(s)   (multiple separated by " ▪ ")
                Category      (e.g. "AI safety & policy")
                Experience    (e.g. "Mid-level" or "5+ years experience")
                "X days ago"
            </a>
          </div>
        """
        # ---- URL --------------------------------------------------------
        link_el = await card.query_selector("a[href*='/job']")
        if link_el is None:
            href = await card.get_attribute("href")
            if href and "/job" in href:
                link_el = card
            else:
                return None

        href = await link_el.get_attribute("href") or ""
        if not href:
            return None
        url = href if href.startswith("http") else f"{_BASE_URL}{href}"

        # ---- Title ------------------------------------------------------
        title = ""
        title_el = await card.query_selector("p.font-bold span")
        if title_el:
            title = (await title_el.inner_text()).strip()
        if not title:
            heading = await card.query_selector("h2, h3, h4")
            if heading:
                title = (await heading.inner_text()).strip()
        if not title:
            return None

        # ---- Parse text lines for company / location / tags -------------
        full_text = (await card.inner_text()).strip()
        lines = [ln.strip() for ln in full_text.split("\n") if ln.strip()]

        # Remove the title itself and "Highlighted role" label
        meta_lines = []
        for ln in lines:
            if ln == title:
                continue
            if ln.lower() in ("highlighted role", "new"):
                continue
            meta_lines.append(ln)

        # Identify the "X days/hours ago" line — always last
        posted_at = None
        if meta_lines and re.search(r"\d+\s+(day|hour|minute|week|month)s?\s+ago", meta_lines[-1], re.I):
            posted_at = self._parse_relative_time(meta_lines.pop())

        # After removing time, typical remaining order:
        # [Company, Location(s), Category, Experience]
        # Location lines often contain commas (city, country) or " ▪ "
        company = meta_lines[0] if len(meta_lines) >= 1 else "Unknown"
        location = meta_lines[1] if len(meta_lines) >= 2 else "Remote"
        tags: list[str] = meta_lines[2:4] if len(meta_lines) > 2 else []

        # Salary — look for currency/number patterns
        salary = None
        for ln in meta_lines:
            if re.search(r"[\$€£]\s*\d[\d,]*", ln):
                salary = ln.strip()
                break

        # Remote scope heuristic
        remote_scope = "worldwide"
        loc_lower = location.lower()
        if any(kw in loc_lower for kw in ["germany", "berlin", "deutschland"]):
            remote_scope = "germany"
        elif any(kw in loc_lower for kw in ["europe", "eu ", "emea"]):
            remote_scope = "eu"

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=True,
            remote_scope=remote_scope,
            url=url,
            description=None,
            salary=salary,
            tags=tags[:10],
            source=self.name,
            is_ngo=True,
            posted_at=posted_at,
        )

    @staticmethod
    def _parse_relative_time(text: str) -> datetime | None:
        """Convert '2 days ago', '5 hours ago' etc. to a UTC datetime."""
        match = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", text, re.I)
        if not match:
            return None
        from datetime import timedelta
        amount = int(match.group(1))
        unit = match.group(2).lower()
        deltas = {
            "minute": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "month": timedelta(days=amount * 30),
        }
        return datetime.now(timezone.utc) - deltas.get(unit, timedelta())

    def _parse_html_fallback(self, html: str) -> list[Job]:
        """Fallback: parse the rendered HTML with BeautifulSoup."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        # Find all links to job detail pages
        job_links = soup.find_all("a", href=re.compile(r"/job/"))

        seen_urls: set[str] = set()
        for link in job_links:
            href = link.get("href", "")
            url = href if href.startswith("http") else f"{_BASE_URL}{href}"

            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = link.get_text(strip=True)
            if not title or len(title) < 5:
                # Try to find a heading inside
                heading = link.find(["h2", "h3", "h4"])
                if heading:
                    title = heading.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Get parent container for metadata
            parent = link.find_parent(["div", "li", "article", "section"])
            company = "Unknown"
            location = "Remote"
            tags: list[str] = []

            if parent:
                # Try to extract company and location from parent text
                full_text = parent.get_text(" | ", strip=True)
                parts = [p.strip() for p in full_text.split("|") if p.strip()]
                for part in parts:
                    part_lower = part.lower()
                    if any(kw in part_lower for kw in ["remote", "worldwide", "global"]):
                        location = part
                    elif part != title and len(part) > 2:
                        if not company or company == "Unknown":
                            company = part

            try:
                job = Job(
                    title=title,
                    company=company,
                    location=location,
                    is_remote=True,
                    remote_scope="worldwide",
                    url=url,
                    description=None,
                    salary=None,
                    tags=tags,
                    source=self.name,
                    is_ngo=True,
                    posted_at=None,
                )
                jobs.append(job)
            except (ValidationError, Exception) as exc:
                logger.debug("[{}] Skipping fallback job: {}", self.name, exc)

        logger.info("[{}] HTML fallback extracted {} jobs", self.name, len(jobs))
        return jobs
