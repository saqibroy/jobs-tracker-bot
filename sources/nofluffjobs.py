"""NoFluffJobs source — Polish / Central European tech job board.

URL: https://nofluffjobs.com/

Uses the **paginated search API** (POST /api/search/posting) instead of
the bulk listing endpoint, which returns a 150 MB+ JSON blob that blows
past Docker's memory limit.

Each page returns ~300 postings (~2 MB).  We fetch up to
``_MAX_PAGES`` pages and apply client-side filters for remote status
and recency.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_SEARCH_URL = "https://nofluffjobs.com/api/search/posting"

# Categories relevant to software/data engineering
_WANTED_CATEGORIES: set[str] = {
    "backend",
    "fullstack",
    "devops",
    "data",
    "mobile",
    "architecture",
    "security",
    "artificialintelligence",
    "frontend",
    "embedded",
}

# Only consider jobs posted within the last 14 days
_MAX_AGE_MS = 14 * 24 * 3600 * 1000

# How many search pages to fetch (each page ≈ 300 postings, ~2 MB)
_MAX_PAGES = 2


class NoFluffJobsSource(BaseSource):
    """NoFluffJobs — Central European tech board with paginated search API."""

    name = "nofluffjobs"

    async def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        total_scanned = 0

        for page in range(1, _MAX_PAGES + 1):
            postings = await self._fetch_page(page)
            if not postings:
                break

            total_scanned += len(postings)
            new_this_page = 0

            for posting in postings:
                job = self._process_posting(posting, seen_ids, now_ms)
                if job:
                    all_jobs.append(job)
                    new_this_page += 1

            # If most results on this page are too old, stop paging
            if new_this_page == 0:
                break

        logger.info(
            "[{}] Fetched {} remote tech jobs (from {} scanned across {} page(s))",
            self.name, len(all_jobs), total_scanned, min(page, _MAX_PAGES),
        )
        return all_jobs

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _fetch_page(self, page: int) -> list[dict]:
        """Fetch one page from the search endpoint."""
        try:
            resp = await self._post(
                _SEARCH_URL,
                json_body={
                    "criteriaSearch": {
                        "category": list(_WANTED_CATEGORIES),
                    },
                    "page": page,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params={
                    "salaryCurrency": "EUR",
                    "salaryPeriod": "month",
                },
            )
        except Exception as exc:
            logger.error("[{}] Search page {} failed: {}", self.name, page, exc)
            return []

        if resp.status_code != 200:
            logger.warning("[{}] Search page {} returned {}", self.name, page, resp.status_code)
            return []

        data = resp.json()
        return data.get("postings") or []

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _process_posting(
        self, posting: dict, seen_ids: set[str], now_ms: int
    ) -> Job | None:
        """Apply client-side filters and parse one posting."""
        # Category filter
        category = (posting.get("category") or "").lower()
        if category not in _WANTED_CATEGORIES:
            return None

        # Remote: either fullyRemote flag or "Remote" city in places
        if not self._is_remote(posting):
            return None

        # Recency
        posted_ts = posting.get("posted") or 0
        if posted_ts and (now_ms - posted_ts) > _MAX_AGE_MS:
            return None

        # Dedup
        posting_id = posting.get("id", "")
        if posting_id in seen_ids:
            return None
        seen_ids.add(posting_id)

        try:
            return self._parse_posting(posting)
        except (ValidationError, KeyError, TypeError) as exc:
            logger.debug("[{}] Skipping posting: {}", self.name, exc)
            return None

    # ------------------------------------------------------------------
    # Remote detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_remote(posting: dict) -> bool:
        """Check whether the posting is fully remote."""
        if posting.get("fullyRemote", False):
            return True

        location_data = posting.get("location") or {}
        if location_data.get("fullyRemote", False):
            return True

        # Search API marks some remote jobs with city="Remote"
        places = location_data.get("places") or []
        for place in places:
            city = (place.get("city") or "").strip().lower()
            if city == "remote":
                return True

        return False

    # ------------------------------------------------------------------
    # Posting -> Job
    # ------------------------------------------------------------------

    def _parse_posting(self, posting: dict) -> Job | None:
        """Convert a single NoFluffJobs posting into a Job."""
        title = (posting.get("title") or "").strip()
        if not title:
            return None

        # URL — English version
        url_slug = posting.get("url") or posting.get("id") or ""
        if not url_slug:
            return None
        url = f"https://nofluffjobs.com/job/{url_slug}"

        company = (posting.get("name") or "Unknown").strip()

        # Location from the nested places structure
        location = self._build_location(posting)

        # Determine remote scope from regions
        regions = posting.get("regions") or []
        remote_scope = self._infer_remote_scope(regions, location)

        # Salary
        salary = self._format_salary(posting.get("salary"))

        # Tags from seniority + technology + category
        tags: list[str] = []
        seniority = posting.get("seniority") or []
        if isinstance(seniority, list):
            tags.extend(seniority)
        tech = posting.get("technology")
        if tech:
            tags.append(tech)
        category = posting.get("category")
        if category:
            tags.append(category)
        tags = tags[:10]

        # Posted date (epoch ms)
        posted_at = None
        ts = posting.get("posted")
        if ts and isinstance(ts, (int, float)):
            try:
                posted_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            except (OSError, ValueError):
                pass

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=True,
            remote_scope=remote_scope,
            url=url,
            description=None,
            salary=salary,
            tags=tags,
            source=self.name,
            posted_at=posted_at,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_location(posting: dict) -> str:
        """Extract readable location from the posting."""
        location_data = posting.get("location") or {}
        places = location_data.get("places") or []

        cities: list[str] = []
        countries: list[str] = []
        for place in places[:5]:
            city = place.get("city")
            if city and city.lower() != "remote":
                cities.append(city)
            country = (place.get("country") or {}).get("name")
            if country and country not in countries:
                countries.append(country)

        if cities:
            return ", ".join(cities[:3])
        if countries:
            return ", ".join(countries[:3])
        return "Remote"

    @staticmethod
    def _infer_remote_scope(regions: list[str], location: str) -> str:
        """Map NoFluffJobs region codes to remote scope."""
        regions_lower = {r.lower() for r in regions}

        # If multiple EU regions or 'eu' is present
        if len(regions_lower) > 2:
            return "eu"
        if any(r in regions_lower for r in ("eu", "europe")):
            return "eu"

        # Map known country codes
        if regions_lower & {"de", "at", "ch"}:
            return "germany"
        if regions_lower == {"pl"}:
            return "eu"  # Polish remote jobs are typically EU-accessible

        # Default based on location text
        loc_lower = location.lower()
        if any(kw in loc_lower for kw in ("worldwide", "global", "anywhere")):
            return "worldwide"

        return "eu"  # NoFluffJobs is EU-focused by nature

    @staticmethod
    def _format_salary(salary_data: dict | None) -> str | None:
        """Format salary range from API data."""
        if not salary_data:
            return None

        low = salary_data.get("from")
        high = salary_data.get("to")
        currency = salary_data.get("currency", "")
        salary_type = salary_data.get("type", "")

        if not low and not high:
            return None

        parts: list[str] = []
        if low and high:
            parts.append(f"{int(low):,}–{int(high):,}")
        elif low:
            parts.append(f"{int(low):,}+")
        elif high:
            parts.append(f"up to {int(high):,}")

        if currency:
            parts.append(currency)
        if salary_type:
            parts.append(f"({salary_type})")

        return " ".join(parts) if parts else None
