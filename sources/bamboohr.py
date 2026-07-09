"""BambooHR source — public careers-widget JSON (UNOFFICIAL).

BambooHR's documented API requires a per-customer API key and is meant
for HRIS data, not for third parties browsing job postings. The only
public, no-auth surface is the JSON that powers the embeddable careers
widget every BambooHR customer can put on their own site:

  GET https://{slug}.bamboohr.com/careers/list

This is NOT a documented/stable contract — BambooHR can change the shape
or host of this endpoint without notice. Treat this source as best-effort
and keep an eye on the logs; if it starts returning 0 jobs for a company
that has open roles, the endpoint has likely changed.

BambooHR doesn't have a "browse all companies" endpoint — add company
subdomains to BAMBOOHR_COMPANIES in .env.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from models.job import Job
from sources.base import BaseSource

_LIST_URL = "https://{slug}.bamboohr.com/careers/list"


class BambooHRSource(BaseSource):
    name = "bamboohr"

    async def _fetch_company(self, slug: str) -> list[Job]:
        try:
            resp = await self._get(_LIST_URL.format(slug=slug))
        except Exception as exc:
            logger.warning("[{}] Failed to fetch board '{}': {}", self.name, slug, exc)
            return []

        if resp.status_code == 429:
            return []
        if resp.status_code == 404:
            logger.warning("[{}] No such board: '{}' (check BAMBOOHR_COMPANIES)", self.name, slug)
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning(
                "[{}] Non-JSON response for '{}' — endpoint may have changed", self.name, slug
            )
            return []

        # The widget response has historically been either a bare list or
        # {"result": [...]}. Handle both defensively.
        raw_jobs = data if isinstance(data, list) else data.get("result", [])

        jobs: list[Job] = []
        for item in raw_jobs:
            try:
                title = item.get("jobOpeningName") or item.get("title") or ""
                location_obj = item.get("location") or {}
                if isinstance(location_obj, dict):
                    location = ", ".join(
                        v for v in [location_obj.get("city"), location_obj.get("country")] if v
                    ) or "Unspecified"
                else:
                    location = str(location_obj) or "Unspecified"

                is_remote = bool(item.get("isRemote")) or "remote" in str(
                    item.get("locationLabel", "")
                ).lower()

                job_id = item.get("id") or item.get("jobOpeningId") or ""
                url = f"https://{slug}.bamboohr.com/careers/{job_id}" if job_id else f"https://{slug}.bamboohr.com/careers"

                job = Job(
                    title=title,
                    company=slug,
                    location=location,
                    is_remote=is_remote,
                    url=url,
                    description=item.get("description", "") or "",
                    tags=[t for t in [item.get("department")] if t],
                    source=self.name,
                )
                jobs.append(job)
            except (AttributeError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry for '{}': {}", self.name, slug, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        import config

        if not config.BAMBOOHR_COMPANIES:
            logger.debug("[{}] No BAMBOOHR_COMPANIES configured — skipping", self.name)
            return []

        tasks = [self._fetch_company(slug) for slug in config.BAMBOOHR_COMPANIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[Job] = []
        for slug, result in zip(config.BAMBOOHR_COMPANIES, results):
            if isinstance(result, Exception):
                logger.warning("[{}] Board '{}' failed: {}", self.name, slug, result)
                continue
            all_jobs.extend(result)

        logger.info(
            "[{}] Fetched {} jobs from {} companies",
            self.name, len(all_jobs), len(config.BAMBOOHR_COMPANIES),
        )
        return all_jobs
