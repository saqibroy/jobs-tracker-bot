"""Greenhouse public Job Board API adapter."""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.ats_common import (
    clean_html, country_codes_from_text, infer_workplace, parse_datetime,
    regions_from_text,
)
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for

_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseSource(BaseSource):
    name = "greenhouse"

    async def _fetch_board(self, board: CompanyBoard) -> list[Job]:
        response = await self._get(_URL.format(slug=board.slug), params={"content": "true"})
        self._require_component_response(response)
        jobs = []
        for item in response.json().get("jobs", []):
            try:
                location = (item.get("location") or {}).get("name") or "Unspecified"
                description = clean_html(item.get("content"))
                evidence = f"{location} {description}"
                workplace = infer_workplace(evidence)
                jobs.append(Job(
                    title=item.get("title") or "",
                    company=board.company,
                    location=location,
                    is_remote=workplace in ("remote", "hybrid"),
                    workplace_type=workplace,
                    eligible_countries=country_codes_from_text(location),
                    eligible_regions=regions_from_text(location),
                    url=item.get("absolute_url") or "",
                    description=description,
                    tags=[
                        value.get("name", "") for value in
                        (item.get("departments") or []) + (item.get("offices") or [])
                        if value.get("name")
                    ],
                    source=self.name,
                    posted_at=parse_datetime(item.get("updated_at")),
                ))
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                logger.debug(
                    "[{}] Skipping malformed listing for '{}': {}",
                    self.name, board.slug, exc,
                )
        return jobs

    async def fetch(self) -> list[Job]:
        boards = boards_for(self.name)
        results = await self._map_bounded(boards, self._fetch_board)
        jobs = self._consume_component_results(
            boards, results, lambda board: f"board:{board.slug}"
        )
        return jobs
