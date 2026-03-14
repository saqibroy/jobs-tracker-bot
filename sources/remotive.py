"""Remotive.com source — free JSON API.

Endpoint: https://remotive.com/api/remote-jobs?category=software-dev
Returns JSON with a "jobs" array.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveSource(BaseSource):
    name = "remotive"

    async def fetch(self) -> list[Job]:
        resp = await self._get(_API_URL, params={"category": "software-dev"})

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
