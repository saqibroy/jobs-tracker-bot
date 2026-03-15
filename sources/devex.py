"""Devex source — JSON API scraper.

API: https://www.devex.com/api/public/search/jobs

International development sector, many NGO/INGO tech roles.
All Devex listings are from the development sector, so ``is_ngo=True``.

Method: httpx GET → parse JSON API response.
The listing page loads jobs via JS, but the public API returns structured
JSON with title, company, location, published_at, topics, etc.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://www.devex.com"
_API_URL = f"{_BASE_URL}/api/public/search/jobs"

# Fetch 2 pages of 10 results (max 20 jobs)
_MAX_PAGES = 2

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class DevexSource(BaseSource):
    name = "devex"

    async def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_ids: set[int] = set()

        for page_num in range(1, _MAX_PAGES + 1):
            params = {
                "filter[job_types][]": "1",      # job type = job
                "filter[remote_type][]": "1",     # remote
                "page[number]": str(page_num),
            }

            try:
                resp = await self._get(_API_URL, params=params, headers=_HEADERS)
            except Exception as exc:
                logger.error("[{}] Request failed (page {}): {}", self.name, page_num, exc)
                break

            if resp.status_code == 429:
                logger.warning("[{}] Rate limited on page {}", self.name, page_num)
                break

            if resp.status_code != 200:
                logger.warning("[{}] HTTP {} on page {}", self.name, resp.status_code, page_num)
                break

            try:
                data = resp.json()
            except Exception:
                logger.error("[{}] Invalid JSON response on page {}", self.name, page_num)
                break

            entries = data.get("data", [])
            if not entries:
                break

            for entry in entries:
                try:
                    job = self._parse_entry(entry, seen_ids)
                    if job:
                        all_jobs.append(job)
                except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                    logger.debug("[{}] Skipping malformed entry: {}", self.name, exc)

            # Check if there are more pages
            page_info = data.get("page", {})
            if page_num >= page_info.get("pages", 1):
                break

        if not all_jobs:
            logger.warning("[{}] No jobs parsed from API", self.name)

        return all_jobs

    def _parse_entry(self, entry: dict, seen_ids: set[int]) -> Job | None:
        """Parse a single job entry from the Devex JSON API."""
        job_id = entry.get("id")
        if not job_id or job_id in seen_ids:
            return None
        seen_ids.add(job_id)

        # ── Title ─────────────────────────────────────────────────────
        title = entry.get("name", "").strip()
        if not title or len(title) < 5:
            return None

        # ── URL ───────────────────────────────────────────────────────
        slug = entry.get("slug_and_id", "")
        url = f"{_BASE_URL}/jobs/{slug}" if slug else f"{_BASE_URL}/jobs/{job_id}"

        # ── Company ───────────────────────────────────────────────────
        employer = entry.get("employer_company", {})
        company = employer.get("name", "Unknown") if employer else "Unknown"

        # ── Location ─────────────────────────────────────────────────
        places = entry.get("places", [])
        location = self._build_location(places)

        # ── Tags from topics ─────────────────────────────────────────
        tags: list[str] = []
        for topic in entry.get("news_topics", []):
            name = topic.get("name", "")
            if name:
                tags.append(name)

        # ── Published date ───────────────────────────────────────────
        posted_at = None
        published = entry.get("published_at", "")
        if published:
            try:
                posted_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # ── Remote status ────────────────────────────────────────────
        is_remote = entry.get("is_remote", False)
        # The API filter already selects remote jobs but the field can
        # be False for remote-type listings; we mark as remote since
        # the search filter specifies remote_type=1
        if not is_remote:
            is_remote = True

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=is_remote,
            url=url,
            description=None,
            salary=None,
            tags=tags[:10],
            source=self.name,
            is_ngo=True,  # All Devex listings are development sector
            posted_at=posted_at,
        )

    @staticmethod
    def _build_location(places: list[dict]) -> str:
        """Build a human-readable location from the places array.

        The array typically contains city, country, and region entries.
        We prefer city+country, falling back to just country or region.
        """
        city = ""
        country = ""
        region = ""

        for place in places:
            ptype = place.get("type", "")
            name = place.get("name", "")
            if ptype == "City" and not city:
                city = name
            elif ptype == "Country" and not country:
                country = name
            elif ptype == "Region" and not region:
                region = name

        if city and country:
            return f"{city}, {country}"
        elif country:
            return country
        elif region:
            return region
        elif city:
            return city
        return "Remote"

    # Keep these for test compatibility
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
        """Fallback: find containers with links to job detail pages."""
        results = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if re.search(r"/jobs/\d+|/jobs/[a-z0-9-]+", href):
                if "/jobs/search" in href or "filter" in href:
                    continue
                parent = a.find_parent(["div", "li", "article", "section"])
                if parent and parent not in results:
                    results.append(parent)
                elif a not in results:
                    results.append(a)
        return results
