"""WeWorkRemotely source — RSS feed.

Endpoint: https://weworkremotely.com/categories/remote-programming-jobs.rss
Standard RSS 2.0 with <item> entries.

Fields per item:
  title, link, guid, pubDate, description (HTML), category, region
"""

from __future__ import annotations

from datetime import datetime, timezone

import feedparser
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_RSS_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


class WeWorkRemotelySource(BaseSource):
    name = "weworkremotely"

    async def fetch(self) -> list[Job]:
        resp = await self._get(_RSS_URL)

        if resp.status_code == 429:
            return []

        feed = feedparser.parse(resp.text)
        jobs: list[Job] = []

        for entry in feed.entries:
            try:
                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Extract company from title — WWR format is often "Company: Job Title"
                company = "Unknown"
                if ": " in title:
                    company, title = title.split(": ", 1)

                # URL
                url = entry.get("link", "") or entry.get("guid", "")
                if not url:
                    continue

                # Publication date
                posted_at = None
                published = entry.get("published_parsed")
                if published:
                    try:
                        posted_at = datetime(
                            *published[:6], tzinfo=timezone.utc
                        )
                    except (ValueError, TypeError):
                        pass

                # Location — from <region> tag
                location = entry.get("region", "") or "Remote"

                # Description (HTML content)
                description = entry.get("description", "") or entry.get("summary", "")

                # Category / tags
                tags = []
                category = entry.get("category", "")
                if category:
                    tags.append(category)

                job = Job(
                    title=title,
                    company=company,
                    location=location,
                    is_remote=True,  # WWR is a remote-only board
                    url=url,
                    description=description,
                    salary=None,  # Not reliably available in RSS
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry: {}", self.name, exc)
                continue

        return jobs
