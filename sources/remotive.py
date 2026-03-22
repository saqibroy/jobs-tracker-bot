"""Remotive.com source — free JSON API (multiple categories).

Fetches from multiple Remotive categories in parallel and deduplicates
by URL before returning.

Endpoints:
  - https://remotive.com/api/remote-jobs?category=software-dev&limit=100
  - https://remotive.com/api/remote-jobs?category=devops-sysadmin&limit=100
  - https://remotive.com/api/remote-jobs?category=data&limit=100
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://remotive.com/api/remote-jobs"

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
        try:
            resp = await self._get(
                _API_URL, params={"category": category, "limit": 100}
            )
        except Exception as exc:
            logger.warning("[{}] Failed to fetch category {}: {}", self.name, category, exc)
            return []

        if resp.status_code == 429:
            return []

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

                job = Job(
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    location=item.get("candidate_required_location", "Anywhere"),
                    is_remote=True,  # Remotive is remote-only board
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    salary=item.get("salary", None) or None,
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry: {}", self.name, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        """Fetch all categories in parallel and deduplicate by URL."""
        tasks = [self._fetch_category(cat) for cat in _CATEGORIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for result in results:
            if isinstance(result, Exception):
                logger.warning("[{}] Category fetch failed: {}", self.name, result)
                continue
            for job in result:
                if job.url not in seen_urls:
                    seen_urls.add(job.url)
                    all_jobs.append(job)

        logger.info(
            "[{}] Fetched {} unique jobs from {} categories",
            self.name, len(all_jobs), len(_CATEGORIES),
        )
        return all_jobs
