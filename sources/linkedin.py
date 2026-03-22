"""LinkedIn Jobs source — guest API (no auth required).

Uses LinkedIn's public guest API which returns HTML fragments:
  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?...

Parameters:
  keywords   — search query
  location   — e.g. "Germany", "Europe"
  f_WT=2     — remote only
  f_TPR=r604800 — last 7 days
  start      — pagination offset (0, 10, 20, ...)

Multiple search queries are run in parallel and results are deduplicated
by URL.

NOTE: LinkedIn may return 429 or redirect to login at any time. The source
gracefully handles this by logging a warning and returning [].
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# Multiple search queries to maximise coverage
_SEARCH_QUERIES: list[dict[str, str]] = [
    {"keywords": "software engineer", "location": "Germany"},
    {"keywords": "fullstack developer", "location": "Germany"},
    {"keywords": "backend developer", "location": "Europe"},
    {"keywords": "frontend developer", "location": "Europe"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Pattern to extract relative time from LinkedIn (e.g. "5 days ago", "1 day ago")
_TIME_AGO_RE = re.compile(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", re.IGNORECASE)


def _parse_relative_time(text: str) -> datetime | None:
    """Parse '5 days ago' style text into a UTC datetime."""
    match = _TIME_AGO_RE.search(text)
    if not match:
        return None
    from datetime import timedelta

    amount = int(match.group(1))
    unit = match.group(2).lower()
    now = datetime.now(timezone.utc)

    if unit == "minute":
        return now - timedelta(minutes=amount)
    elif unit == "hour":
        return now - timedelta(hours=amount)
    elif unit == "day":
        return now - timedelta(days=amount)
    elif unit == "week":
        return now - timedelta(weeks=amount)
    elif unit == "month":
        return now - timedelta(days=amount * 30)
    return None


class LinkedInSource(BaseSource):
    name = "linkedin"

    async def _fetch_query(self, query: dict[str, str]) -> list[Job]:
        """Fetch a single LinkedIn search query and parse the HTML results."""
        params = {
            "keywords": query["keywords"],
            "location": query["location"],
            "f_WT": "2",          # remote only
            "f_TPR": "r604800",   # last 7 days
            "start": "0",
        }

        try:
            resp = await self._get(_BASE_URL, params=params, headers=_HEADERS)
        except Exception as exc:
            logger.warning("[{}] Request failed for '{}': {}", self.name, query["keywords"], exc)
            return []

        if resp.status_code == 429:
            logger.warning("[{}] Rate limited — skipping", self.name)
            return []

        # Check for login redirect (LinkedIn blocks guest access sometimes)
        if "login" in resp.text[:500].lower() or resp.status_code in (301, 302, 403):
            logger.warning("[{}] LinkedIn redirected to login — guest API blocked", self.name)
            return []

        return self._parse_html(resp.text)

    def _parse_html(self, html: str) -> list[Job]:
        """Parse LinkedIn guest API HTML fragment into Job objects."""
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        # LinkedIn returns <li> items with class "jobs-search-results__list-item"
        # or <div class="base-card"> — try both patterns
        cards = soup.find_all("div", class_="base-card")
        if not cards:
            cards = soup.find_all("li")

        for card in cards:
            try:
                # Title
                title_el = card.find("h3", class_="base-search-card__title")
                if not title_el:
                    title_el = card.find("h3")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title:
                    continue

                # Company
                company_el = card.find("h4", class_="base-search-card__subtitle")
                if not company_el:
                    company_el = card.find("h4")
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                # URL
                link_el = card.find("a", class_="base-card__full-link")
                if not link_el:
                    link_el = card.find("a", href=True)
                if not link_el or not link_el.get("href"):
                    continue
                url = link_el["href"].split("?")[0]  # strip tracking params
                if not url.startswith("http"):
                    url = f"https://www.linkedin.com{url}"

                # Location
                location_el = card.find("span", class_="job-search-card__location")
                location = location_el.get_text(strip=True) if location_el else "Remote"

                # Time posted
                time_el = card.find("time")
                posted_at = None
                if time_el:
                    # Try datetime attribute first
                    dt_attr = time_el.get("datetime")
                    if dt_attr:
                        try:
                            posted_at = datetime.fromisoformat(
                                dt_attr.replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            pass
                    # Fallback to text parsing
                    if not posted_at:
                        posted_at = _parse_relative_time(time_el.get_text())

                job = Job(
                    title=title,
                    company=company,
                    location=location,
                    is_remote=True,  # f_WT=2 filters for remote
                    url=url,
                    description="",  # Guest API doesn't include descriptions
                    salary=None,
                    tags=[],
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(job)

            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                logger.debug("[{}] Skipping malformed card: {}", self.name, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        """Fetch all search queries in parallel and deduplicate by URL."""
        tasks = [self._fetch_query(q) for q in _SEARCH_QUERIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for result in results:
            if isinstance(result, Exception):
                logger.warning("[{}] Query failed: {}", self.name, result)
                continue
            for job in result:
                if job.url not in seen_urls:
                    seen_urls.add(job.url)
                    all_jobs.append(job)

        logger.info(
            "[{}] Fetched {} unique jobs from {} queries",
            self.name, len(all_jobs), len(_SEARCH_QUERIES),
        )
        return all_jobs
