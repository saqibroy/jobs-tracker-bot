"""Personio ATS source — public, unauthenticated XML job feed.

Personio doesn't have a "browse all companies" endpoint — each employer's
career site exposes its own XML feed. Add company subdomains to
PERSONIO_COMPANIES in .env (the part before ".jobs.personio.de").

Endpoint (no auth required):
  GET https://{slug}.jobs.personio.de/xml?language=en

Feed schema (workzag-jobs / position):
  id, office, department, recruitingCategory, name, employmentType,
  seniority, schedule, yearsOfExperience, keywords, occupation,
  occupationCategory, createdAt, jobDescriptions/jobDescription[]
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime

from loguru import logger

from models.job import Job
from sources.base import BaseSource
from sources.ats_common import country_codes_from_text, infer_workplace, regions_from_text
from sources.registry import CompanyBoard, boards_for

_XML_URL = "https://{slug}.jobs.personio.de/xml"

_REMOTE_HINT_TOKENS = ("remote", "home office", "hybrid", "homeoffice")


class PersonioSource(BaseSource):
    name = "personio"

    async def _fetch_company(self, board: CompanyBoard | str) -> list[Job]:
        if isinstance(board, str):
            board = CompanyBoard(company=board, provider=self.name, slug=board)
        slug = board.slug
        try:
            resp = await self._get(_XML_URL.format(slug=slug), params={"language": "en"})
        except Exception as exc:
            logger.warning("[{}] Failed to fetch feed '{}': {}", self.name, slug, exc)
            return []

        if resp.status_code == 429:
            return []
        if resp.status_code == 404:
            logger.warning("[{}] No such feed: '{}' (check PERSONIO_COMPANIES)", self.name, slug)
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            logger.warning("[{}] Malformed XML for '{}': {}", self.name, slug, exc)
            return []

        jobs: list[Job] = []
        for position in root.findall("position"):
            try:
                pid = (position.findtext("id") or "").strip()
                title = (position.findtext("name") or "").strip()
                office = (position.findtext("office") or "").strip()
                department = (position.findtext("department") or "").strip()
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
                jobs.append(job)
            except (AttributeError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry for '{}': {}", self.name, slug, exc)
                continue

        return jobs

    async def fetch(self) -> list[Job]:
        boards = boards_for(self.name)
        if not boards:
            logger.debug("[{}] No enabled company boards — skipping", self.name)
            return []

        results = await self._map_bounded(boards, self._fetch_company)

        all_jobs: list[Job] = []
        for board, result in zip(boards, results):
            if isinstance(result, Exception):
                logger.warning("[{}] Feed '{}' failed: {}", self.name, board.slug, result)
                continue
            all_jobs.extend(result)

        logger.info(
            "[{}] Fetched {} jobs from {} companies",
            self.name, len(all_jobs), len(boards),
        )
        return all_jobs
