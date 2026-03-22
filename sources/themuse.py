"""The Muse source — large job platform with public API.

URL: https://www.themuse.com/

The Muse offers a well-documented public API (no key required) with
category and location filtering.  We query for remote Software
Engineering, Data Science, and IT roles.

API endpoint:
    GET https://www.themuse.com/api/public/jobs
    Query params: page, category, location, level
    No authentication required.

Pagination: 20 results per page, up to page_count pages.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://www.themuse.com/api/public/jobs"

# Categories to query (separate API calls per category)
_CATEGORIES: list[str] = [
    "Software Engineering",
    "Data Science",
    "IT",
    "Data and Analytics",
]

_REMOTE_LOCATION = "Flexible / Remote"
_MAX_PAGES = 5  # 100 jobs per category max


class TheMuseSource(BaseSource):
    """The Muse — public job API with category/location filtering."""

    name = "themuse"

    async def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_ids: set[int] = set()

        for category in _CATEGORIES:
            jobs = await self._fetch_category(category, seen_ids)
            all_jobs.extend(jobs)

        logger.info("[{}] Fetched {} unique remote jobs", self.name, len(all_jobs))
        return all_jobs

    async def _fetch_category(self, category: str, seen_ids: set[int]) -> list[Job]:
        """Fetch all pages for a single category."""
        jobs: list[Job] = []

        for page in range(1, _MAX_PAGES + 1):
            params = {
                "category": category,
                "location": _REMOTE_LOCATION,
                "page": page,
            }

            try:
                resp = await self._get(_API_URL, params=params)
            except Exception as exc:
                logger.error(
                    "[{}] API request failed (category='{}', page={}): {}",
                    self.name, category, page, exc,
                )
                break

            if resp.status_code != 200:
                logger.warning(
                    "[{}] API returned {} for category='{}'",
                    self.name, resp.status_code, category,
                )
                break

            data = resp.json()
            results = data.get("results") or []
            if not results:
                break

            for item in results:
                job_id = item.get("id")
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                try:
                    job = self._parse_result(item)
                    if job:
                        jobs.append(job)
                except (ValidationError, KeyError, TypeError) as exc:
                    logger.debug("[{}] Skipping result: {}", self.name, exc)

            # Stop if last page
            page_count = data.get("page_count", 0)
            if page >= page_count:
                break

        return jobs

    # ------------------------------------------------------------------
    # Result -> Job
    # ------------------------------------------------------------------

    def _parse_result(self, item: dict) -> Job | None:
        """Convert a single Muse API result into a Job."""
        title = (item.get("name") or "").strip()
        if not title:
            return None

        # URL from refs
        refs = item.get("refs") or {}
        url = refs.get("landing_page") or ""
        if not url:
            return None

        # Company
        company_data = item.get("company") or {}
        company = (company_data.get("name") or "Unknown").strip()

        # Locations
        locations = item.get("locations") or []
        location = self._build_location(locations)
        remote_scope = self._infer_remote_scope(locations)

        # Tags from categories + levels
        tags: list[str] = []
        for cat in (item.get("categories") or []):
            name = cat.get("name") if isinstance(cat, dict) else str(cat)
            if name:
                tags.append(name)
        for level in (item.get("levels") or []):
            name = level.get("name") if isinstance(level, dict) else str(level)
            if name:
                tags.append(name)
        tags = tags[:10]

        # Published date
        posted_at = None
        pub_str = item.get("publication_date") or ""
        if pub_str:
            try:
                posted_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=True,
            remote_scope=remote_scope,
            url=url,
            description=None,
            salary=None,
            tags=tags,
            source=self.name,
            posted_at=posted_at,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_location(locations: list[dict]) -> str:
        """Build location string from Muse location objects."""
        names = []
        for loc in locations:
            name = loc.get("name") if isinstance(loc, dict) else str(loc)
            if name and name.lower() not in ("flexible / remote", "remote"):
                names.append(name)

        if names:
            return ", ".join(names[:3])
        return "Remote"

    @staticmethod
    def _infer_remote_scope(locations: list[dict]) -> str:
        """Infer remote scope from location names."""
        all_names = " ".join(
            (loc.get("name") or "") if isinstance(loc, dict) else str(loc)
            for loc in locations
        ).lower()

        if any(kw in all_names for kw in ("germany", "berlin", "munich", "hamburg")):
            return "germany"
        if any(kw in all_names for kw in (
            "europe", "london", "amsterdam", "paris", "brussels",
            "dublin", "lisbon", "barcelona", "madrid",
        )):
            return "eu"

        # The Muse is US-heavy, but "Flexible / Remote" usually means worldwide
        return "worldwide"
