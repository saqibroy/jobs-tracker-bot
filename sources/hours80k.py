"""80,000 Hours Job Board — Algolia API source.

URL: https://jobs.80000hours.org/

The job board is a Nuxt.js SPA backed by Algolia search.  We query
Algolia directly with the public application-id and search-only key
that the frontend exposes in its ``__NUXT__`` config.

All 80,000 Hours jobs are from the Effective Altruism / impact sector,
so ``is_ngo=True`` for all listings.

When location is unclear, we default ``remote_scope="worldwide"``
because 80k Hours jobs are often worldwide remote or EU-accessible.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

# Algolia credentials (public, exposed in the site's __NUXT__ config)
_ALGOLIA_APP_ID = "W6KM1UDIB3"
_ALGOLIA_API_KEY = "d1d7f2c8696e7b36837d5ed337c4a319"
_ALGOLIA_INDEX = "jobs_prod"
_ALGOLIA_URL = f"https://{_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query"

_HITS_PER_PAGE = 100
_MAX_PAGES = 3  # 300 jobs max — more than enough for a scan


class Hours80kSource(BaseSource):
    """80,000 Hours job board via Algolia search API."""

    name = "hours80k"

    async def fetch(self) -> list[Job]:
        headers = {
            "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
            "X-Algolia-API-Key": _ALGOLIA_API_KEY,
            "Content-Type": "application/json",
        }

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for page in range(_MAX_PAGES):
            payload = {
                "query": "",
                "hitsPerPage": _HITS_PER_PAGE,
                "page": page,
            }

            try:
                resp = await self._post(
                    _ALGOLIA_URL,
                    headers=headers,
                    json_body=payload,
                )
            except Exception as exc:
                logger.error("[{}] Algolia request failed (page {}): {}", self.name, page, exc)
                break

            if resp.status_code != 200:
                logger.warning("[{}] Algolia returned {} on page {}", self.name, resp.status_code, page)
                break

            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                break

            for hit in hits:
                try:
                    job = self._parse_hit(hit)
                    if job and job.url not in seen_urls:
                        seen_urls.add(job.url)
                        all_jobs.append(job)
                except (ValidationError, KeyError, TypeError) as exc:
                    logger.debug("[{}] Skipping hit: {}", self.name, exc)

            # Stop if this was the last page
            nb_pages = data.get("nbPages", 0)
            if page + 1 >= nb_pages:
                break

        logger.info("[{}] Fetched {} jobs from Algolia ({} pages)", self.name, len(all_jobs), page + 1)

        # Pre-filter: remove non-dev roles specific to 80k Hours board
        pre_count = len(all_jobs)
        all_jobs = [j for j in all_jobs if self._is_relevant_for_user(j)]
        filtered_count = pre_count - len(all_jobs)
        if filtered_count > 0:
            logger.info("[{}] Pre-filtered {} non-dev roles", self.name, filtered_count)

        return all_jobs

    # ------------------------------------------------------------------
    # Pre-filter for 80k Hours (non-dev roles)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_relevant_for_user(job: Job) -> bool:
        """Filter out non-dev roles specific to the 80k Hours board.

        80k Hours lists research grants, policy roles, lab positions,
        and funding calls alongside real engineering jobs.  This
        pre-filter keeps only roles with a dev signal in the title.
        """
        title_lower = job.title.lower()

        # Hard reject: these are not software engineering jobs
        _REJECT_PATTERNS = [
            "request for proposals", "rfp",
            "research laboratory", "lab technician", "benchside",
            "mathematical model", "quantitative",
            "wet lab", "clinical",
            "policy analyst", "policy researcher",
            "programme officer", "program officer",
            "operations lead", "operations manager",
            "communications", "outreach",
            "fellowship",
            "internship",
        ]

        if any(p in title_lower for p in _REJECT_PATTERNS):
            return False

        # For 80k Hours, require at least one dev signal in title
        _DEV_SIGNALS = [
            "engineer", "developer", "architect", "devops",
            "fullstack", "full stack", "full-stack",
            "frontend", "front-end", "front end",
            "backend", "back-end", "back end",
            "software", "platform", "infrastructure", "sre",
            "data engineer",
        ]

        return any(s in title_lower for s in _DEV_SIGNALS)

    # ------------------------------------------------------------------
    # Hit -> Job
    # ------------------------------------------------------------------

    def _parse_hit(self, hit: dict) -> Job | None:
        """Convert a single Algolia hit into a Job."""
        title = (hit.get("title") or "").strip()
        if not title:
            return None

        url = (hit.get("url_external") or "").strip()
        if not url:
            return None

        company = (hit.get("company_name") or "Unknown").strip()

        # Location — combine city + country tags
        cities = hit.get("card_locations") or hit.get("tags_city") or []
        countries = hit.get("tags_country") or []
        location = self._build_location(cities, countries)

        # Remote scope
        remote_scope = self._infer_remote_scope(location, cities, countries)
        is_remote = True  # 80k Hours board is remote-heavy

        # Tags — combine area + skill tags
        tags: list[str] = []
        for key in ("tags_area", "tags_skill", "tags_role_type"):
            vals = hit.get(key) or []
            tags.extend(vals)
        tags = tags[:10]

        # Posted date
        posted_at = None
        ts = hit.get("posted_at")
        if ts and isinstance(ts, (int, float)):
            try:
                posted_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, ValueError):
                pass

        # Salary
        salary = hit.get("salary")
        if salary and isinstance(salary, str):
            salary = salary.strip() or None
        else:
            salary = None

        # Description snippet
        desc = hit.get("description_short") or ""
        if desc:
            # Strip HTML tags from the snippet
            desc = re.sub(r"<[^>]+>", " ", desc).strip()
            desc = desc[:2000]

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=is_remote,
            remote_scope=remote_scope,
            url=url,
            description=desc or None,
            salary=salary,
            tags=tags,
            source=self.name,
            is_ngo=True,
            posted_at=posted_at,
        )

    # ------------------------------------------------------------------
    # Location helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_location(cities: list[str], countries: list[str]) -> str:
        """Combine city + country lists into a human-readable location."""
        if cities:
            return ", ".join(cities[:3])
        if countries:
            return ", ".join(countries[:3])
        return "Remote"

    @staticmethod
    def _infer_remote_scope(location: str, cities: list, countries: list) -> str:
        """Determine remote scope from location tags."""
        combined = " ".join(cities + countries).lower()
        loc_lower = location.lower()

        if any(kw in combined or kw in loc_lower for kw in [
            "remote, global", "worldwide", "global",
        ]):
            return "worldwide"
        if any(kw in combined or kw in loc_lower for kw in [
            "germany", "berlin", "deutschland", "munich", "hamburg",
        ]):
            return "germany"
        if any(kw in combined or kw in loc_lower for kw in [
            "europe", "eu ", "emea", "brussels", "amsterdam",
        ]):
            return "eu"
        # Default to worldwide for 80k Hours jobs
        return "worldwide"
