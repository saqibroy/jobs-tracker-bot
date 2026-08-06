"""Ashby ATS source — public, unauthenticated job-board API.

Ashby doesn't have a "browse all companies" endpoint — each employer's
board is fetched individually by its slug (the last path segment of
https://jobs.ashbyhq.com/<slug>). Add slugs to ASHBY_COMPANIES in .env.

Endpoint (no auth required):
  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger

from models.job import Job
from sources.base import BaseSource
from sources.ats_common import country_codes_from_text, infer_workplace, regions_from_text
from sources.registry import CompanyBoard, boards_for

_API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource(BaseSource):
    name = "ashby"

    async def _fetch_company(self, board: CompanyBoard | str) -> list[Job]:
        if isinstance(board, str):
            board = CompanyBoard(company=board, provider=self.name, slug=board)
        slug = board.slug
        resp = await self._get(
            _API_URL.format(slug=slug), params={"includeCompensation": "false"}
        )
        self._require_component_response(resp)

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

                workplace_type = infer_workplace(
                    f"{location} {item.get('workplaceType') or ''}",
                    bool(item.get("isRemote")),
                )
                is_remote = workplace_type in ("remote", "hybrid")

                tags = [t for t in [item.get("department"), item.get("team")] if t]

                job = Job(
                    title=item.get("title", ""),
                    company=board.company,
                    location=location,
                    is_remote=is_remote,
                    workplace_type=workplace_type,
                    eligible_countries=country_codes_from_text(location),
                    eligible_regions=regions_from_text(location),
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
        boards = boards_for(self.name)
        if not boards:
            logger.debug("[{}] No enabled company boards — skipping", self.name)
            return []

        results = await self._map_bounded(boards, self._fetch_company)

        all_jobs = self._consume_component_results(
            boards, results, lambda board: f"board:{board.slug}"
        )

        logger.info(
            "[{}] Fetched {} jobs from {} companies",
            self.name, len(all_jobs), len(boards),
        )
        return all_jobs
