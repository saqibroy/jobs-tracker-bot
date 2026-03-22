"""NoFluffJobs source — Polish / Central European tech job board.

URL: https://nofluffjobs.com/

NoFluffJobs is one of the largest tech job boards in Central Europe,
focused on Poland but with listings across the EU.  Their public API
returns **all** postings in a single call (~20k+), so we filter
client-side by category and remote status.

API endpoint:
    GET https://nofluffjobs.com/api/posting
    No authentication required.

Categories we care about: backend, fullstack, devops, data, mobile,
architecture, security, artificialIntelligence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://nofluffjobs.com/api/posting"

# Categories relevant to software/data engineering
_WANTED_CATEGORIES: set[str] = {
    "backend",
    "fullstack",
    "devops",
    "data",
    "mobile",
    "architecture",
    "security",
    "artificialIntelligence",
    "frontend",
    "embedded",
}

# Only consider jobs posted within the last 14 days
_MAX_AGE_MS = 14 * 24 * 3600 * 1000


class NoFluffJobsSource(BaseSource):
    """NoFluffJobs — Central European tech board with public JSON API."""

    name = "nofluffjobs"

    async def fetch(self) -> list[Job]:
        try:
            resp = await self._get(
                _API_URL,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            logger.error("[{}] API request failed: {}", self.name, exc)
            return []

        if resp.status_code != 200:
            logger.warning("[{}] API returned {}", self.name, resp.status_code)
            return []

        data = resp.json()
        postings = data.get("postings") or []
        if not postings:
            logger.warning("[{}] No postings in API response", self.name)
            return []

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        for posting in postings:
            # Client-side filtering: category + remote
            category = (posting.get("category") or "").lower()
            if category not in _WANTED_CATEGORIES:
                continue

            # Must be fully remote
            is_remote = posting.get("fullyRemote", False)
            if not is_remote:
                location_data = posting.get("location") or {}
                is_remote = location_data.get("fullyRemote", False)
            if not is_remote:
                continue

            # Skip old postings (>14 days)
            posted_ts = posting.get("posted") or 0
            if posted_ts and (now_ms - posted_ts) > _MAX_AGE_MS:
                continue

            # Dedup by posting ID
            posting_id = posting.get("id", "")
            if posting_id in seen_ids:
                continue
            seen_ids.add(posting_id)

            try:
                job = self._parse_posting(posting)
                if job:
                    all_jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.debug("[{}] Skipping posting: {}", self.name, exc)

        logger.info(
            "[{}] Fetched {} remote tech jobs (from {} total postings)",
            self.name, len(all_jobs), len(postings),
        )
        return all_jobs

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
