"""Direct public career-page adapter using schema.org JobPosting JSON-LD."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.ats_common import clean_html, country_codes_from_text, infer_workplace, parse_datetime, regions_from_text
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for


def _job_postings(value) -> list[dict]:
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_job_postings(item))
        return result
    if not isinstance(value, dict):
        return []
    if value.get("@type") == "JobPosting":
        return [value]
    result = []
    for key in ("@graph", "itemListElement", "mainEntity"):
        if key in value:
            result.extend(_job_postings(value[key]))
    return result


class JsonLdCareerSource(BaseSource):
    name = "jsonld"

    async def _fetch_board(self, board: CompanyBoard) -> list[Job]:
        response = await self._get(board.url)
        self._require_component_response(response)
        soup = BeautifulSoup(response.text, "html.parser")
        postings: list[dict] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                postings.extend(_job_postings(json.loads(script.string or script.get_text())))
            except (json.JSONDecodeError, TypeError):
                continue
        jobs = []
        for item in postings:
            try:
                location_value = item.get("jobLocation") or item.get("applicantLocationRequirements") or ""
                location = clean_html(json.dumps(location_value, ensure_ascii=False))
                workplace = infer_workplace(
                    f"{location} {item.get('jobLocationType', '')}",
                    item.get("jobLocationType") == "TELECOMMUTE",
                )
                jobs.append(Job(
                    title=item.get("title") or "",
                    company=board.company,
                    location=location or "Unspecified",
                    is_remote=workplace in ("remote", "hybrid"),
                    workplace_type=workplace,
                    eligible_countries=country_codes_from_text(location),
                    eligible_regions=regions_from_text(location),
                    url=item.get("url") or board.url,
                    description=clean_html(item.get("description")),
                    tags=[str(item.get("employmentType", ""))],
                    source=self.name,
                    posted_at=parse_datetime(item.get("datePosted")),
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
            boards, results, lambda board: f"career:{board.slug or board.url}"
        )
