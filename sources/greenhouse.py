"""Greenhouse public Job Board API adapter."""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from filters.employment import (
    EmploymentStructuredInput,
    classify_employment,
    merge_structured_employment_inputs,
)
from models.job import Job
from sources.ats_common import (
    clean_html, country_codes_from_text, infer_workplace, parse_datetime,
    regions_from_text,
)
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for

_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

_TIME_TYPE_MAP = {
    "full time": EmploymentStructuredInput(work_schedule="full_time"),
    "full-time": EmploymentStructuredInput(work_schedule="full_time"),
    "part time": EmploymentStructuredInput(work_schedule="part_time"),
    "part-time": EmploymentStructuredInput(work_schedule="part_time"),
}
_EMPLOYMENT_TYPE_MAP = {
    **_TIME_TYPE_MAP,
    "unlimited contract": EmploymentStructuredInput(contract_term="permanent"),
    "permanent": EmploymentStructuredInput(contract_term="permanent"),
    "fixed term": EmploymentStructuredInput(contract_term="fixed_term"),
    "fixed-term": EmploymentStructuredInput(contract_term="fixed_term"),
    "intern": EmploymentStructuredInput(employment_relationship="internship"),
    "working student": EmploymentStructuredInput(
        employment_relationship="working_student"
    ),
}


def _greenhouse_employment(
    metadata: object,
) -> tuple[EmploymentStructuredInput, dict[str, str]]:
    if not isinstance(metadata, list):
        return EmploymentStructuredInput(), {}

    mapped_fields: list[tuple[str, EmploymentStructuredInput]] = []
    for entry in metadata:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        mapping = {
            "time type": _TIME_TYPE_MAP,
            "employment type": _EMPLOYMENT_TYPE_MAP,
        }.get(name.strip().lower())
        if mapping is None:
            continue
        raw_values = entry.get("value")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            if not isinstance(value, str):
                continue
            mapped = mapping.get(value.strip().lower())
            if mapped is not None:
                mapped_fields.append((f"metadata.{name}", mapped))

    structured = merge_structured_employment_inputs(
        *(mapped for _field, mapped in mapped_fields)
    )
    fields: dict[str, str] = {}
    for dimension in (
        "employment_relationship",
        "work_schedule",
        "contract_term",
    ):
        final_value = getattr(structured, dimension)
        if final_value is None:
            continue
        field = next(
            field
            for field, mapped in mapped_fields
            if getattr(mapped, dimension) == final_value
        )
        fields[dimension] = field
    return structured, fields


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
                job = Job(
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
                )
                structured, fields = _greenhouse_employment(item.get("metadata"))
                jobs.append(classify_employment(
                    job,
                    structured,
                    structured_source=self.name,
                    structured_fields=fields,
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
