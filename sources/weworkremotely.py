"""WeWorkRemotely source — RSS feeds (multiple categories).

Fetches from all developer-related RSS feeds in parallel and deduplicates
by URL before returning.

Endpoints:
  - https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss
  - https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss
  - https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss
  - https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss
  - https://weworkremotely.com/categories/remote-programming-jobs.rss
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import feedparser
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_RSS_FEEDS: list[str] = [
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]


class WeWorkRemotelySource(BaseSource):
    name = "weworkremotely"

    async def _fetch_feed(self, feed_url: str) -> list[Job]:
        """Fetch and parse a single RSS feed, returning Job objects."""
        try:
            resp = await self._get(feed_url)
        except Exception as exc:
            logger.warning("[{}] Failed to fetch {}: {}", self.name, feed_url, exc)
            return []

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

    async def fetch(self) -> list[Job]:
        """Fetch all RSS feeds in parallel and deduplicate by URL."""
        tasks = [self._fetch_feed(url) for url in _RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for result in results:
            if isinstance(result, Exception):
                logger.warning("[{}] Feed fetch failed: {}", self.name, result)
                continue
            for job in result:
                if job.url not in seen_urls:
                    seen_urls.add(job.url)
                    all_jobs.append(job)

        logger.info("[{}] Fetched {} unique jobs from {} feeds", self.name, len(all_jobs), len(_RSS_FEEDS))
        return all_jobs
