"""Ashby ATS source — public, unauthenticated job-board API.

Ashby doesn't have a "browse all companies" endpoint — each employer's
board is fetched individually by its slug (the last path segment of
https://jobs.ashbyhq.com/<slug>). Add slugs to ASHBY_COMPANIES in .env.

Endpoint (no auth required):
  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

import config
from models.job import Job
from sources.base import BaseSource

_API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource(BaseSource):
    name = "ashby"

    async def _fetch_company(self, slug: str) -> list[Job]:
        try:
            resp = await self._get(
                _API_URL.format(slug=slug), params={"includeCompensation": "false"}
            )
        except Exception as exc:
            logger.warning("[{}] Failed to fetch board '{}': {}", self.name, slug, exc)
            return []

        if resp.status_code == 429:
            return []
        if resp.status_code == 404:
            logger.warning("[{}] No such board: '{}' (check ASHBY_COMPANIES)", self.name, slug)
            return []

        data = resp.json()
        jobs: list[Job] = []

        for item in data.get("jobs", []):
            try:
                posted_at = None
                pub_date = item.get("publishedAt")
                if pub_date:
                    try:
                        posted_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                # Build a location string from the primary + secondary
                # locations so downstream location filtering sees every
                # country the role is open to.
                location_parts = [item.get("location", "")]
                for sec in item.get("secondaryLocations", []) or []:
                    loc_name = sec.get("location") if isinstance(sec, dict) else None
                    if loc_name:
                        location_parts.append(loc_name)
                location = ", ".join(p for p in location_parts if p) or "Unspecified"

                is_remote = bool(item.get("isRemote")) or (
                    (item.get("workplaceType") or "").lower() in ("remote", "hybrid")
                )

                tags = [t for t in [item.get("department"), item.get("team")] if t]

                job = Job(
                    title=item.get("title", ""),
                    company=slug,
                    location=location,
                    is_remote=is_remote,
                    url=item.get("jobUrl") or item.get("applyUrl") or "",
                    description=item.get("descriptionPlain") or "",
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(job)
            except (KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry for '{}': {}", self.name, slug, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        if not config.ASHBY_COMPANIES:
            logger.debug("[{}] No ASHBY_COMPANIES configured — skipping", self.name)
            return []

        tasks = [self._fetch_company(slug) for slug in config.ASHBY_COMPANIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[Job] = []
        for slug, result in zip(config.ASHBY_COMPANIES, results):
            if isinstance(result, Exception):
                logger.warning("[{}] Board '{}' failed: {}", self.name, slug, result)
                continue
            all_jobs.extend(result)

        logger.info(
            "[{}] Fetched {} jobs from {} companies",
            self.name, len(all_jobs), len(config.ASHBY_COMPANIES),
        )
        return all_jobs
