"""Lever public Postings API adapter (global and EU instances)."""

from __future__ import annotations

import asyncio

from loguru import logger

from models.job import Job
from sources.ats_common import clean_html, country_codes_from_text, infer_workplace, regions_from_text
from sources.base import BaseSource
from sources.registry import CompanyBoard, boards_for


class LeverSource(BaseSource):
    name = "lever"

    async def _fetch_board(self, board: CompanyBoard) -> list[Job]:
        host = "api.eu.lever.co" if board.region.lower() == "eu" else "api.lever.co"
        response = await self._get(
            f"https://{host}/v0/postings/{board.slug}",
            params={"mode": "json"},
            headers={"Accept": "application/json"},
        )
        jobs = []
        for item in response.json():
            categories = item.get("categories") or {}
            locations = categories.get("allLocations") or [categories.get("location", "")]
            location = ", ".join(value for value in locations if value) or "Unspecified"
            description = clean_html(
                " ".join([
                    item.get("descriptionPlain") or item.get("description") or "",
                    item.get("additionalPlain") or item.get("additional") or "",
                ])
            )
            workplace = infer_workplace(f"{location} {item.get('workplaceType', '')}")
            jobs.append(Job(
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
                        categories.get("commitment"), item.get("workplaceType"),
                    ) if value
                ],
                source=self.name,
            ))
        return jobs

    async def fetch(self) -> list[Job]:
        boards = boards_for(self.name)
        results = await self._map_bounded(boards, self._fetch_board)
        jobs: list[Job] = []
        for board, result in zip(boards, results):
            if isinstance(result, Exception):
                logger.warning("[{}] Board {} failed: {}", self.name, board.slug, result)
            else:
                jobs.extend(result)
        return jobs
