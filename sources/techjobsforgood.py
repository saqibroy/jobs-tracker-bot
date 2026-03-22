"""Tech Jobs for Good source — httpx + BeautifulSoup scraper.

URL: https://www.techjobsforgood.com/jobs/?q=&job_type=full-time&remote=on

Best intersection of tech + NGO/impact.  Highly targeted board — all
jobs are from impact-driven organisations, so ``is_ngo=True`` for all.

Method: httpx GET → parse HTML job cards with BeautifulSoup.

Location note: Tech Jobs for Good is US-heavy but has worldwide/EU
remote roles.  We set ``is_ngo=True`` and let the standard location
filter handle EU/worldwide acceptance.

Note: This site uses Cloudflare.  From datacenter IPs (cloud servers)
the request may be blocked.  From residential IPs or with a proxy it
works fine.  We detect the block and log a warning.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://www.techjobsforgood.com"
_LISTING_URL = f"{_BASE_URL}/jobs/?q=&job_type=full-time&remote=on"

# User-Agent to reduce Cloudflare challenges
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class TechJobsForGoodSource(BaseSource):
    name = "techjobsforgood"

    async def fetch(self) -> list[Job]:
        html = await self._fetch_html()
        if not html or len(html) < 500:
            logger.warning("[{}] Empty or very short response — possibly blocked", self.name)
            return []

        # Detect hard Cloudflare block (IP-based, not solvable)
        if "you have been blocked" in html.lower() or "cf-error-details" in html:
            logger.warning(
                "[{}] Cloudflare hard block — site blocks cloud/DC IPs. "
                "This source only works from residential IPs or with a proxy.",
                self.name,
            )
            return []

        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        # Try multiple selectors — site structure may vary
        job_cards = (
            soup.select("div.job-listing")
            or soup.select("div.job-card")
            or soup.select("article.job")
            or soup.select("div.listing")
            or soup.select("li.job-item")
            or soup.select("[class*='job']")
        )

        if not job_cards:
            # Fallback: look for anchor tags to /jobs/ detail pages
            job_cards = self._extract_job_links_fallback(soup)

        for card in job_cards:
            try:
                job = self._parse_card(card)
                if job:
                    jobs.append(job)
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                logger.debug("[{}] Skipping malformed card: {}", self.name, exc)

        if not jobs:
            logger.warning(
                "[{}] No jobs parsed from HTML ({} chars, {} card elements)",
                self.name, len(html), len(job_cards),
            )

        return jobs

    async def _fetch_html(self) -> str:
        """Fetch HTML via httpx."""
        try:
            resp = await self._get(_LISTING_URL, headers=_HEADERS)
            if resp.status_code == 429:
                logger.warning("[{}] Rate-limited (429)", self.name)
                return ""
            return resp.text
        except Exception as exc:
            logger.error("[{}] httpx fetch failed: {}", self.name, exc)
            return ""

    def _parse_card(self, card) -> Job | None:
        """Parse a single job card element into a Job."""
        # ── Title + URL ────────────────────────────────────────────────
        link_el = card.select_one("a[href*='/jobs/']") or card.find("a", href=True)
        if not link_el:
            return None

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")

        if not title or not href:
            return None

        # Some titles are wrapped in h2/h3 inside the link
        if not title:
            heading = card.select_one("h2, h3, h4")
            if heading:
                title = heading.get_text(strip=True)

        url = href if href.startswith("http") else f"{_BASE_URL}{href}"

        # ── Company ────────────────────────────────────────────────────
        company = self._extract_text(card, [
            "span.company", "div.company", "span.org", "div.org",
            "span.company-name", "div.company-name",
            "[class*='company']", "[class*='org']",
        ]) or "Unknown"

        # ── Location ──────────────────────────────────────────────────
        location = self._extract_text(card, [
            "span.location", "div.location", "span.loc",
            "[class*='location']", "[class*='remote']",
        ]) or "Remote"

        # ── Tags / skills ─────────────────────────────────────────────
        tags: list[str] = []
        tag_els = card.select("span.tag, span.skill, span.badge, [class*='tag'], [class*='skill']")
        for el in tag_els:
            text = el.get_text(strip=True)
            if text and len(text) < 50:
                tags.append(text)

        # ── Description snippet ───────────────────────────────────────
        description = self._extract_text(card, [
            "p.description", "div.description", "p.summary",
            "[class*='description']", "[class*='summary']",
        ]) or ""

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=True,
            url=url,
            description=description[:5000] if description else None,
            salary=None,
            tags=tags[:10],
            source=self.name,
            is_ngo=True,  # All Tech Jobs for Good listings are impact-driven
            posted_at=None,
        )

    @staticmethod
    def _extract_text(element, selectors: list[str]) -> str | None:
        """Try multiple CSS selectors and return first non-empty text."""
        for sel in selectors:
            el = element.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
        return None

    @staticmethod
    def _extract_job_links_fallback(soup) -> list:
        """Fallback: find all anchors pointing to job detail pages."""
        results = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            # Match /jobs/<id> or /jobs/<slug> style URLs
            if re.search(r"/jobs?/\d+|/jobs?/[a-z0-9-]+", href):
                # Use the parent container as the "card"
                parent = a.find_parent(["div", "li", "article", "section"])
                if parent and parent not in results:
                    results.append(parent)
                elif a not in results:
                    results.append(a)
        return results
