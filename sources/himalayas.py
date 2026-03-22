"""Himalayas.app source — large remote-first job aggregator.

URL: https://himalayas.app/

Himalayas aggregates 90,000+ remote jobs worldwide.  The public API
returns 20 jobs per page with offset-based pagination.  We collect
multiple pages and filter for EU/worldwide-accessible roles.

API endpoint:
    GET https://himalayas.app/jobs/api?limit=20&offset=N
    No authentication required.

Location restrictions are provided per job as a list of country names,
which we use to determine remote_scope.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://himalayas.app/jobs/api"
_PAGE_SIZE = 20  # API hard-caps at 20
_MAX_PAGES = 10  # 200 jobs max

# Parent categories / keywords in category names we want
_WANTED_CATEGORY_KEYWORDS: set[str] = {
    "software",
    "engineering",
    "fullstack",
    "backend",
    "frontend",
    "devops",
    "infrastructure",
    "data",
    "machine-learning",
    "cloud",
    "security",
    "sre",
    "platform",
    "systems",
    "developer",
    "development",
}

# EU / worldwide location keywords for filtering
_EU_COUNTRIES: set[str] = {
    "germany", "france", "netherlands", "spain", "italy", "belgium",
    "austria", "switzerland", "portugal", "ireland", "denmark", "sweden",
    "norway", "finland", "poland", "czech republic", "romania", "hungary",
    "greece", "croatia", "united kingdom", "uk", "luxembourg",
}


class HimalayasSource(BaseSource):
    """Himalayas.app — large remote job aggregator with public API."""

    name = "himalayas"

    async def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            try:
                resp = await self._get(
                    _API_URL,
                    params={"limit": _PAGE_SIZE, "offset": offset},
                )
            except Exception as exc:
                logger.error("[{}] API request failed (offset={}): {}", self.name, offset, exc)
                break

            if resp.status_code != 200:
                logger.warning("[{}] API returned {} at offset {}", self.name, resp.status_code, offset)
                break

            data = resp.json()
            jobs_data = data.get("jobs") or []
            if not jobs_data:
                break

            for item in jobs_data:
                # Filter: must be a relevant tech category
                if not self._is_wanted_category(item):
                    continue

                # Filter: must be EU-accessible or worldwide
                if not self._is_eu_accessible(item):
                    continue

                try:
                    job = self._parse_job(item)
                    if job and job.url not in seen_urls:
                        seen_urls.add(job.url)
                        all_jobs.append(job)
                except (ValidationError, KeyError, TypeError) as exc:
                    logger.debug("[{}] Skipping job: {}", self.name, exc)

        logger.info("[{}] Fetched {} EU-relevant tech jobs", self.name, len(all_jobs))
        return all_jobs

    # ------------------------------------------------------------------
    # Job parsing
    # ------------------------------------------------------------------

    def _parse_job(self, item: dict) -> Job | None:
        """Convert a Himalayas API job into a Job."""
        title = (item.get("title") or "").strip()
        if not title:
            return None

        url = (item.get("applicationLink") or item.get("guid") or "").strip()
        if not url:
            return None

        company = (item.get("companyName") or "Unknown").strip()

        # Location from restrictions
        restrictions = item.get("locationRestrictions") or []
        location = ", ".join(restrictions[:3]) if restrictions else "Remote (Worldwide)"

        # Remote scope
        remote_scope = self._infer_remote_scope(restrictions)

        # Salary
        salary = self._format_salary(item)

        # Tags from categories + seniority
        tags: list[str] = []
        for cat in (item.get("categories") or []):
            tags.append(cat.replace("-", " "))
        for level in (item.get("seniority") or []):
            tags.append(level)
        employment = item.get("employmentType")
        if employment:
            tags.append(employment)
        tags = tags[:10]

        # Posted date (epoch seconds)
        posted_at = None
        ts = item.get("pubDate")
        if ts and isinstance(ts, (int, float)):
            try:
                posted_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, ValueError):
                pass

        # Description snippet
        desc = (item.get("excerpt") or "").strip()
        if desc:
            desc = desc[:2000]

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=True,
            remote_scope=remote_scope,
            url=url,
            description=desc or None,
            salary=salary,
            tags=tags,
            source=self.name,
            posted_at=posted_at,
        )

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_wanted_category(item: dict) -> bool:
        """Check if the job is in a software/data/infra category."""
        categories = item.get("categories") or []
        for cat in categories:
            cat_lower = cat.lower().replace("-", " ")
            for kw in _WANTED_CATEGORY_KEYWORDS:
                if kw.replace("-", " ") in cat_lower:
                    return True
        # Also check title as fallback
        title = (item.get("title") or "").lower()
        tech_title_kw = {"engineer", "developer", "devops", "sre", "backend", "frontend", "fullstack", "data", "architect", "platform"}
        return any(kw in title for kw in tech_title_kw)

    @staticmethod
    def _is_eu_accessible(item: dict) -> bool:
        """Check if job is worldwide or accessible from EU."""
        restrictions = item.get("locationRestrictions") or []

        # No restrictions = worldwide
        if not restrictions:
            return True

        restrictions_lower = {r.lower() for r in restrictions}

        # Check for EU countries or worldwide keywords
        if restrictions_lower & {"worldwide", "global", "anywhere", "remote"}:
            return True

        return bool(restrictions_lower & _EU_COUNTRIES)

    @staticmethod
    def _infer_remote_scope(restrictions: list[str]) -> str:
        """Determine remote scope from location restrictions."""
        if not restrictions:
            return "worldwide"

        restrictions_lower = {r.lower() for r in restrictions}

        if restrictions_lower & {"worldwide", "global", "anywhere"}:
            return "worldwide"

        eu_matches = restrictions_lower & _EU_COUNTRIES
        if eu_matches:
            # Multiple EU countries → EU scope
            if len(eu_matches) > 1:
                return "eu"
            # Single country: Germany alone → germany scope
            if eu_matches == {"germany"}:
                return "germany"
            return "eu"

        # US-only or other non-EU
        return "worldwide"  # Let the location filter sort it out

    @staticmethod
    def _format_salary(item: dict) -> str | None:
        """Format salary from min/max fields."""
        low = item.get("minSalary")
        high = item.get("maxSalary")
        currency = item.get("currency", "USD")

        if not low and not high:
            return None
        if low == 0 and high == 0:
            return None

        if low and high and low != high:
            return f"{int(low):,}–{int(high):,} {currency}"
        if low:
            return f"{int(low):,} {currency}"
        if high:
            return f"{int(high):,} {currency}"
        return None
