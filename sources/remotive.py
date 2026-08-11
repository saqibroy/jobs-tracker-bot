"""Remotive.com source — free JSON API (multiple categories).

Fetches from multiple Remotive categories in parallel and deduplicates
by URL before returning.

Endpoints:
  - https://remotive.com/api/remote-jobs?category=software-dev&limit=100
  - https://remotive.com/api/remote-jobs?category=devops-sysadmin&limit=100
  - https://remotive.com/api/remote-jobs?category=data&limit=100
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from filters.employment import EmploymentStructuredInput, classify_employment
from models.job import Job
from sources.base import BaseSource
from sources.ats_common import country_codes_from_text, regions_from_text

_API_URL = "https://remotive.com/api/remote-jobs"

_JOB_TYPE_MAP = {
    "full_time": EmploymentStructuredInput(work_schedule="full_time"),
    "part_time": EmploymentStructuredInput(work_schedule="part_time"),
    "freelance": EmploymentStructuredInput(employment_relationship="freelance"),
}


def _remotive_employment(value: object) -> EmploymentStructuredInput:
    if not isinstance(value, str):
        return EmploymentStructuredInput()
    return _JOB_TYPE_MAP.get(value.strip().lower(), EmploymentStructuredInput())

# Categories to fetch — role filter handles non-dev roles from broader categories
_CATEGORIES: list[str] = [
    "software-dev",
    "devops-sysadmin",
    "data",
]


class RemotiveSource(BaseSource):
    name = "remotive"

    async def _fetch_category(self, category: str) -> list[Job]:
        """Fetch and parse jobs from a single Remotive category."""
        resp = await self._get(
            _API_URL, params={"category": category, "limit": 100}
        )
        self._require_component_response(resp)

        data = resp.json()
        raw_jobs = data.get("jobs", [])
        jobs: list[Job] = []

        for item in raw_jobs:
            try:
                # Parse the publication date
                posted_at = None
                pub_date = item.get("publication_date")
                if pub_date:
                    try:
                        posted_at = datetime.fromisoformat(
                            pub_date.replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass

                # Build tags from the API's candidate_required_location + tags
                tags = []
                if item.get("tags"):
                    tags = item["tags"] if isinstance(item["tags"], list) else []

                required_location = item.get("candidate_required_location", "Anywhere")
                job = Job(
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    location=required_location,
                    is_remote=True,  # Remotive is remote-only board
                    workplace_type="remote",
                    eligible_countries=country_codes_from_text(required_location),
                    eligible_regions=regions_from_text(required_location),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    salary=item.get("salary", None) or None,
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(classify_employment(
                    job,
                    _remotive_employment(item.get("job_type")),
                    structured_source=self.name,
                    structured_fields={
                        "employment_relationship": "job_type",
                        "work_schedule": "job_type",
                    },
                ))
            except (ValidationError, KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry: {}", self.name, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        """Fetch all categories in parallel and deduplicate by URL."""
        results = await self._map_bounded(_CATEGORIES, self._fetch_category)
        category_jobs = self._consume_component_results(
            _CATEGORIES, results, lambda category: f"category:{category}"
        )

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for job in category_jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                all_jobs.append(job)

        logger.info(
            "[{}] Fetched {} unique jobs from {} categories",
            self.name, len(all_jobs), len(_CATEGORIES),
        )
        return all_jobs
