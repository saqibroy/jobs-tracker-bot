"""Landing.jobs source — EU-focused tech job board (Portugal-based).

URL: https://landing.jobs/

Landing.jobs is an EU-focused tech recruitment platform based in Lisbon.
Their public API returns ~50 active listings with excellent structured
data including salary ranges in EUR, tags, and location info.

API endpoint:
    GET https://landing.jobs/api/v1/jobs?limit=100
    No authentication required.

All listings are EU-based by nature — mostly Portugal, but also
other European countries.  Salary is typically in EUR.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://landing.jobs/api/v1/jobs"


class LandingJobsSource(BaseSource):
    """Landing.jobs — EU tech recruitment platform with public API."""

    name = "landingjobs"

    async def fetch(self) -> list[Job]:
        try:
            resp = await self._get(
                _API_URL,
                params={"limit": 100},
            )
        except Exception as exc:
            logger.error("[{}] API request failed: {}", self.name, exc)
            return []

        if resp.status_code != 200:
            logger.warning("[{}] API returned {}", self.name, resp.status_code)
            return []

        postings = resp.json()
        if not isinstance(postings, list):
            logger.warning("[{}] Unexpected response format", self.name)
            return []

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for posting in postings:
            try:
                job = self._parse_posting(posting)
                if job and job.url not in seen_urls:
                    seen_urls.add(job.url)
                    all_jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.debug("[{}] Skipping posting: {}", self.name, exc)

        logger.info("[{}] Fetched {} jobs", self.name, len(all_jobs))
        return all_jobs

    # ------------------------------------------------------------------
    # Posting -> Job
    # ------------------------------------------------------------------

    def _parse_posting(self, posting: dict) -> Job | None:
        """Convert a single Landing.jobs posting into a Job."""
        title = (posting.get("title") or "").strip()
        if not title:
            return None

        url = (posting.get("url") or "").strip()
        if not url:
            return None

        # Company name — not directly in listing API, use URL parsing
        company = self._extract_company(url)

        # Location from locations array
        locations = posting.get("locations") or []
        location = self._build_location(locations)

        # Remote status
        is_remote = posting.get("remote", False)
        remote_scope = self._infer_remote_scope(locations, is_remote)

        # Salary
        salary = self._format_salary(posting)

        # Tags
        tags = posting.get("tags") or []
        if isinstance(tags, list):
            tags = [str(t).strip() for t in tags if t][:10]
        else:
            tags = []

        job_type = posting.get("type")
        if job_type and job_type not in tags:
            tags.append(job_type)

        # Posted date
        posted_at = None
        pub_str = posting.get("published_at") or ""
        if pub_str:
            try:
                posted_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=is_remote,
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
    def _extract_company(url: str) -> str:
        """Extract company name from Landing.jobs URL pattern.

        URLs look like: https://landing.jobs/at/company-name/job-title
        """
        try:
            parts = url.split("/at/")
            if len(parts) > 1:
                slug = parts[1].split("/")[0]
                return slug.replace("-", " ").title()
        except (IndexError, AttributeError):
            pass
        return "Unknown"

    @staticmethod
    def _build_location(locations: list[dict]) -> str:
        """Build location string from locations array."""
        if not locations:
            return "EU (Remote)"

        parts: list[str] = []
        for loc in locations[:3]:
            city = loc.get("city", "")
            country = loc.get("country_code", "")
            if city and country:
                parts.append(f"{city}, {country}")
            elif city:
                parts.append(city)
            elif country:
                parts.append(country)

        return ", ".join(parts) if parts else "EU"

    @staticmethod
    def _infer_remote_scope(locations: list[dict], is_remote: bool) -> str:
        """Determine remote scope from location data."""
        if not locations and is_remote:
            return "eu"  # Landing.jobs is EU-focused

        country_codes = {
            loc.get("country_code", "").upper()
            for loc in locations
            if loc.get("country_code")
        }

        if country_codes & {"DE", "AT", "CH"}:
            return "germany"

        # Landing.jobs is EU-focused, so default to EU scope
        return "eu"

    @staticmethod
    def _format_salary(posting: dict) -> str | None:
        """Format salary from gross_salary_low/high fields."""
        low = posting.get("gross_salary_low")
        high = posting.get("gross_salary_high")
        currency = posting.get("currency_code", "EUR")

        if not low and not high:
            return None

        if low and high:
            return f"{int(low):,}–{int(high):,} {currency}/year"
        if low:
            return f"{int(low):,}+ {currency}/year"
        if high:
            return f"up to {int(high):,} {currency}/year"
        return None
