"""Personio ATS source — public, unauthenticated XML and HTML job feeds.

Personio doesn't have a "browse all companies" endpoint — each employer's
career site exposes its own XML feed. Add company subdomains to
PERSONIO_COMPANIES in .env (the part before ".jobs.personio.de").

Primary endpoint (no auth required):
  GET https://{slug}.jobs.personio.de/xml?language=en

Some live Personio career sites expose a public HTML listing while the XML feed
is unavailable or stale. In that case the source falls back to the public
career page and job detail pages.

Feed schema (workzag-jobs / position):
  id, office, department, recruitingCategory, name, employmentType,
  seniority, schedule, yearsOfExperience, keywords, occupation,
  occupationCategory, createdAt, jobDescriptions/jobDescription[]
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin

import httpx
from loguru import logger
from bs4 import BeautifulSoup
from pydantic import ValidationError

from filters.employment import (
    EmploymentStructuredInput,
    classify_employment,
    merge_structured_employment_inputs,
)
from models.job import Job
from sources.base import BaseSource
from sources.ats_common import clean_html, country_codes_from_text, infer_workplace, regions_from_text
from sources.registry import CompanyBoard, boards_for

_XML_URL = "https://{slug}.jobs.personio.de/xml"
_HTML_URL = "https://{slug}.jobs.personio.de"

_REMOTE_HINT_TOKENS = ("remote", "home office", "hybrid", "homeoffice")
_JOB_LINK_RE = re.compile(r"/job/([A-Za-z0-9_-]+)")
_LOCATION_SPLIT_RE = re.compile(
    r"\b(?:full[- ]time|part[- ]time|permanent employee|temporary|internship|"
    r"working student|freelance|contract|trainee|minijob)\b",
    re.IGNORECASE,
)

_EMPLOYMENT_TYPE_MAP = {
    "permanent": EmploymentStructuredInput(
        employment_relationship="employee", contract_term="permanent"
    ),
    "permanent employee": EmploymentStructuredInput(
        employment_relationship="employee", contract_term="permanent"
    ),
    "festanstellung": EmploymentStructuredInput(
        employment_relationship="employee", contract_term="permanent"
    ),
    "intern": EmploymentStructuredInput(employment_relationship="internship"),
    "internship": EmploymentStructuredInput(employment_relationship="internship"),
    "praktikum": EmploymentStructuredInput(employment_relationship="internship"),
    "working student": EmploymentStructuredInput(
        employment_relationship="working_student"
    ),
    "working_student": EmploymentStructuredInput(
        employment_relationship="working_student"
    ),
    "werkstudent": EmploymentStructuredInput(
        employment_relationship="working_student"
    ),
    "freelance": EmploymentStructuredInput(employment_relationship="freelance"),
    "temporary": EmploymentStructuredInput(contract_term="fixed_term"),
    "fixed term": EmploymentStructuredInput(contract_term="fixed_term"),
    "fixed-term": EmploymentStructuredInput(contract_term="fixed_term"),
    "fixed_term": EmploymentStructuredInput(contract_term="fixed_term"),
    "befristet": EmploymentStructuredInput(contract_term="fixed_term"),
}
_SCHEDULE_MAP = {
    "full-time": EmploymentStructuredInput(work_schedule="full_time"),
    "full time": EmploymentStructuredInput(work_schedule="full_time"),
    "vollzeit": EmploymentStructuredInput(work_schedule="full_time"),
    "part-time": EmploymentStructuredInput(work_schedule="part_time"),
    "part time": EmploymentStructuredInput(work_schedule="part_time"),
    "teilzeit": EmploymentStructuredInput(work_schedule="part_time"),
}


def _personio_employment(
    employment_type: object,
    schedule: object,
    *,
    metadata: list[str] | None = None,
) -> tuple[EmploymentStructuredInput, dict[str, str]]:
    inputs: list[EmploymentStructuredInput] = []
    fields: dict[str, str] = {}

    if isinstance(employment_type, str):
        mapped = _EMPLOYMENT_TYPE_MAP.get(employment_type.strip().lower())
        if mapped is not None:
            inputs.append(mapped)
            for dimension in ("employment_relationship", "contract_term"):
                if getattr(mapped, dimension) is not None:
                    fields[dimension] = "employmentType"

    if isinstance(schedule, str):
        mapped = _SCHEDULE_MAP.get(schedule.strip().lower())
        if mapped is not None:
            inputs.append(mapped)
            fields["work_schedule"] = "schedule"

    for value in metadata or []:
        normalized = value.strip().lower()
        mapped = _SCHEDULE_MAP.get(normalized) or _EMPLOYMENT_TYPE_MAP.get(normalized)
        if mapped is None:
            continue
        inputs.append(mapped)
        for dimension in (
            "employment_relationship",
            "work_schedule",
            "contract_term",
        ):
            if getattr(mapped, dimension) is not None:
                fields.setdefault(dimension, "cardMetadata")

    return merge_structured_employment_inputs(*inputs), fields


class PersonioSource(BaseSource):
    name = "personio"

    async def _fetch_company(self, board: CompanyBoard | str) -> list[Job]:
        if isinstance(board, str):
            board = CompanyBoard(company=board, provider=self.name, slug=board)
        jobs = await self._fetch_xml_company(board)
        if jobs is not None:
            return jobs
        return await self._fetch_html_company(board)

    async def _fetch_xml_company(self, board: CompanyBoard) -> list[Job] | None:
        slug = board.slug
        try:
            resp = await self._get(_XML_URL.format(slug=slug), params={"language": "en"})
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (404, 410) or 300 <= status < 400:
                logger.info(
                    "[{}] XML feed unavailable for '{}' (HTTP {}) — trying HTML fallback",
                    self.name, slug, status,
                )
                return None
            logger.warning("[{}] Failed to fetch feed '{}': {}", self.name, slug, exc)
            raise
        except Exception as exc:
            logger.warning("[{}] Failed to fetch feed '{}': {}", self.name, slug, exc)
            raise

        if resp.status_code == 429:
            self._require_component_response(resp)
        if resp.status_code == 404:
            logger.info("[{}] XML feed missing for '{}' — trying HTML fallback", self.name, slug)
            return None

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            logger.info("[{}] Malformed XML for '{}' — trying HTML fallback: {}", self.name, slug, exc)
            return None

        jobs: list[Job] = []
        for position in root.findall("position"):
            try:
                pid = (position.findtext("id") or "").strip()
                title = (position.findtext("name") or "").strip()
                office = (position.findtext("office") or "").strip()
                department = (position.findtext("department") or "").strip()
                employment_type = (position.findtext("employmentType") or "").strip()
                schedule = (position.findtext("schedule") or "").strip()
                keywords = (position.findtext("keywords") or "").strip()

                # Concatenate all jobDescription blocks (Personio splits
                # the posting into sections like "Responsibilities",
                # "Requirements", "Benefits").
                desc_parts: list[str] = []
                for jd in position.findall("./jobDescriptions/jobDescription"):
                    value = jd.findtext("value") or ""
                    if value.strip():
                        desc_parts.append(value.strip())
                description = "\n\n".join(desc_parts)

                signal_text = f"{office} {schedule} {keywords} {description[:500]}".lower()
                workplace_type = infer_workplace(signal_text)
                is_remote = workplace_type in ("remote", "hybrid")

                created = position.findtext("createdAt")
                posted_at = None
                if created:
                    try:
                        posted_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                url = (
                    f"https://{slug}.jobs.personio.de/job/{pid}"
                    if pid else f"https://{slug}.jobs.personio.de"
                )

                job = Job(
                    title=title,
                    company=board.company,
                    location=office or "Unspecified",
                    is_remote=is_remote,
                    workplace_type=workplace_type,
                    eligible_countries=country_codes_from_text(office),
                    eligible_regions=regions_from_text(office),
                    url=url,
                    description=description,
                    tags=[t for t in [department] if t],
                    source=self.name,
                    posted_at=posted_at,
                )
                structured, fields = _personio_employment(employment_type, schedule)
                jobs.append(classify_employment(
                    job,
                    structured,
                    structured_source=self.name,
                    structured_fields=fields,
                ))
            except (ValidationError, AttributeError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry for '{}': {}", self.name, slug, exc)
                continue

        return jobs

    async def _fetch_html_company(self, board: CompanyBoard) -> list[Job]:
        slug = board.slug
        base_url = _HTML_URL.format(slug=slug)
        resp = await self._get(base_url)
        self._require_component_response(resp)

        links = self._extract_html_job_links(resp.text, base_url)
        if not links:
            logger.warning("[{}] HTML fallback found no jobs for '{}'", self.name, slug)
            return []

        async def fetch_detail(
            item: tuple[str, str, list[str]],
        ) -> Job | None:
            url, card_text, metadata = item
            return await self._fetch_html_detail(board, url, card_text, metadata)

        # This detail boundary owns a local worker pool. The outer board worker
        # holds no permit from this pool, so nested fallback work cannot deadlock.
        results = await self._map_bounded(
            links,
            fetch_detail,
        )

        jobs: list[Job] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[{}] HTML detail failed for '{}': {}", self.name, slug, result)
                continue
            if result:
                jobs.append(result)

        logger.info(
            "[{}] HTML fallback fetched {} jobs for '{}'",
            self.name, len(jobs), slug,
        )
        return jobs

    def _extract_html_job_links(
        self,
        html: str,
        base_url: str,
    ) -> list[tuple[str, str, list[str]]]:
        soup = BeautifulSoup(html, "html.parser")
        by_url: dict[str, tuple[str, list[str]]] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            if not _JOB_LINK_RE.search(href):
                continue
            url = urljoin(base_url, href)
            card_text = anchor.get_text(" ", strip=True)
            metadata = [
                node.get_text(" ", strip=True)
                for node in anchor.select('[class*="jobMetaText"]')
                if node.get_text(" ", strip=True)
            ]
            by_url.setdefault(url, (card_text, metadata))
        return [
            (url, card_text, metadata)
            for url, (card_text, metadata) in sorted(by_url.items())
        ]

    async def _fetch_html_detail(
        self,
        board: CompanyBoard,
        url: str,
        card_text: str,
        metadata: list[str],
    ) -> Job | None:
        try:
            resp = await self._get(url)
            return self._parse_html_detail(board, url, resp.text, card_text, metadata)
        except Exception as exc:
            logger.warning("[{}] Failed to fetch HTML job '{}': {}", self.name, url, exc)
            return self._job_from_card(board, url, card_text, metadata)

    def _parse_html_detail(
        self,
        board: CompanyBoard,
        url: str,
        html: str,
        card_text: str,
        metadata: list[str],
    ) -> Job | None:
        soup = BeautifulSoup(html, "html.parser")

        title_el = (
            soup.select_one("h1.job-position-title")
            or soup.select_one('[class*="jobTitle"]')
            or soup.find("h1")
        )
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            return self._job_from_card(board, url, card_text, metadata)

        location_el = (
            soup.select_one('[class*="JobAttributes_jobMetaItemLocation"]')
            or soup.select_one(".detail-subtitle")
            or soup.select_one('[class*="detail-subtitle"]')
        )
        location = self._clean_html_location(
            location_el.get_text(" ", strip=True) if location_el else ""
        )

        description_el = (
            soup.select_one('[class*="jobDescription"]')
            or soup.select_one(".detail-content-block-conditions")
            or soup.select_one(".detail-content-block")
            or soup.select_one('[class*="detail-content-block"]')
        )
        description = clean_html(str(description_el)) if description_el else ""
        if not description:
            main = soup.find("main") or soup.body
            description = clean_html(str(main)) if main else card_text

        signal_text = f"{title} {location} {description[:1000]}".lower()
        workplace_type = infer_workplace(signal_text)

        job = Job(
            title=title,
            company=board.company,
            location=location or "Unspecified",
            is_remote=workplace_type in ("remote", "hybrid"),
            workplace_type=workplace_type,
            eligible_countries=country_codes_from_text(f"{title} {location}"),
            eligible_regions=regions_from_text(f"{title} {location}"),
            url=url,
            description=description,
            tags=[],
            source=self.name,
        )
        structured, fields = _personio_employment("", "", metadata=metadata)
        return classify_employment(
            job,
            structured,
            structured_source=self.name,
            structured_fields=fields,
        )

    def _job_from_card(
        self,
        board: CompanyBoard,
        url: str,
        card_text: str,
        metadata: list[str],
    ) -> Job | None:
        text = " ".join(card_text.split())
        if not text:
            return None
        # Personio cards usually render as "Title Location Schedule Type".
        # Keep this conservative: the fallback preserves the job as raw input,
        # while downstream role/location filters decide whether it is usable.
        location = self._clean_html_location(text)
        workplace_type = infer_workplace(text)
        job = Job(
            title=text,
            company=board.company,
            location=location or "Unspecified",
            is_remote=workplace_type in ("remote", "hybrid"),
            workplace_type=workplace_type,
            eligible_countries=country_codes_from_text(text),
            eligible_regions=regions_from_text(text),
            url=url,
            description=text,
            tags=[],
            source=self.name,
        )
        structured, fields = _personio_employment("", "", metadata=metadata)
        return classify_employment(
            job,
            structured,
            structured_source=self.name,
            structured_fields=fields,
        )

    def _clean_html_location(self, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            return ""
        text = _LOCATION_SPLIT_RE.split(text, maxsplit=1)[0]
        return text.strip(" ·,-|")

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
