"""Workable public published-job endpoint adapter."""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.ats_common import clean_html, country_codes_from_text, infer_workplace, parse_datetime, regions_from_text
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for

_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


class WorkableSource(BaseSource):
    name = "workable"

    async def _fetch_board(self, board: CompanyBoard) -> list[Job]:
        response = await self._get(_URL.format(slug=board.slug), params={"details": "true"})
        self._require_component_response(response)
        payload = response.json()
        raw_jobs = payload.get("jobs", payload.get("results", [])) if isinstance(payload, dict) else payload
        jobs = []
        for item in raw_jobs or []:
            try:
                location_data = item.get("location") or {}
                location = (
                    location_data.get("location_str") if isinstance(location_data, dict)
                    else str(location_data)
                ) or item.get("location_str") or "Unspecified"
                workplace_raw = (
                    location_data.get("workplace_type", "") if isinstance(location_data, dict)
                    else ""
                )
                workplace = infer_workplace(
                    f"{location} {workplace_raw}",
                    location_data.get("telecommuting") if isinstance(location_data, dict) else None,
                )
                description = clean_html(item.get("description") or item.get("full_description"))
                jobs.append(Job(
                    title=item.get("title") or item.get("full_title") or "",
                    company=board.company,
                    location=location,
                    is_remote=workplace in ("remote", "hybrid"),
                    workplace_type=workplace,
                    eligible_countries=country_codes_from_text(location),
                    eligible_regions=regions_from_text(location),
                    url=item.get("url") or item.get("shortlink") or "",
                    description=description,
                    tags=[value for value in (item.get("department"), item.get("employment_type")) if value],
                    source=self.name,
                    posted_at=parse_datetime(item.get("created_at")),
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
        return self._consume_component_results(
            boards, results, lambda board: f"board:{board.slug}"
        )
