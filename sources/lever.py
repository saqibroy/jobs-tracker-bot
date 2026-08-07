"""Lever public Postings API adapter (global and EU instances)."""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from filters.employment import EmploymentStructuredInput, classify_employment
from models.job import Job
from sources.ats_common import clean_html, country_codes_from_text, infer_workplace, regions_from_text
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for

_COMMITMENT_MAP = {
    "full-time": EmploymentStructuredInput(work_schedule="full_time"),
    "full time": EmploymentStructuredInput(work_schedule="full_time"),
    "full-time (remote)": EmploymentStructuredInput(work_schedule="full_time"),
    "part-time": EmploymentStructuredInput(work_schedule="part_time"),
    "part time": EmploymentStructuredInput(work_schedule="part_time"),
    "intern": EmploymentStructuredInput(employment_relationship="internship"),
    "internship": EmploymentStructuredInput(employment_relationship="internship"),
    "werkstudent": EmploymentStructuredInput(
        employment_relationship="working_student"
    ),
    "fixed term contract": EmploymentStructuredInput(contract_term="fixed_term"),
    "temporary": EmploymentStructuredInput(contract_term="fixed_term"),
    "permanent full time employee": EmploymentStructuredInput(
        employment_relationship="employee",
        work_schedule="full_time",
        contract_term="permanent",
    ),
}


def _lever_employment(value: object) -> EmploymentStructuredInput:
    if not isinstance(value, str):
        return EmploymentStructuredInput()
    return _COMMITMENT_MAP.get(value.strip().lower(), EmploymentStructuredInput())


class LeverSource(BaseSource):
    name = "lever"

    async def _fetch_board(self, board: CompanyBoard) -> list[Job]:
        host = "api.eu.lever.co" if board.region.lower() == "eu" else "api.lever.co"
        response = await self._get(
            f"https://{host}/v0/postings/{board.slug}",
            params={"mode": "json"},
            headers={"Accept": "application/json"},
        )
        self._require_component_response(response)
        jobs = []
        for item in response.json():
            try:
                categories = item.get("categories") or {}
                locations = categories.get("allLocations") or [categories.get("location", "")]
                location = ", ".join(value for value in locations if value) or "Unspecified"
                commitment = categories.get("commitment")
                description = clean_html(
                    " ".join([
                        item.get("descriptionPlain") or item.get("description") or "",
                        item.get("additionalPlain") or item.get("additional") or "",
                    ])
                )
                workplace = infer_workplace(f"{location} {item.get('workplaceType', '')}")
                job = Job(
                    title=item.get("text") or "",
                    company=board.company,
                    location=location,
                    is_remote=workplace in ("remote", "hybrid"),
                    workplace_type=workplace,
                    eligible_countries=country_codes_from_text(location),
                    eligible_regions=regions_from_text(location),
                    url=item.get("hostedUrl") or item.get("applyUrl") or "",
                    description=description,
                    tags=[
                        value for value in (
                            categories.get("team"), categories.get("department"),
                            commitment, item.get("workplaceType"),
                        ) if isinstance(value, str) and value
                    ],
                    source=self.name,
                )
                jobs.append(classify_employment(
                    job,
                    _lever_employment(commitment),
                    structured_source=self.name,
                    structured_fields={
                        "employment_relationship": "commitment",
                        "work_schedule": "commitment",
                        "contract_term": "commitment",
                    },
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
