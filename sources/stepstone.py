"""Stepstone / Arbeitsagentur source — German job market via public API.

The Bundesagentur fuer Arbeit (German Federal Employment Agency) exposes
a free JSON API that aggregates postings from Stepstone, Indeed, and
the agency's own board.  No authentication beyond a public API key.

API endpoint:
    GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs
    Header: X-API-Key: jobboerse-jobsuche

We run multiple queries for different tech keywords to maximise
coverage, and merge / deduplicate by ``refnr`` (reference number).

Location scope is always ``germany`` — this is the German job market.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
_API_KEY = "jobboerse-jobsuche"

# Queries to run — covers developer roles in both English and German
_SEARCH_QUERIES: list[dict[str, str]] = [
    {"was": "Software Entwickler", "wo": "Deutschland"},
    {"was": "Software Developer", "wo": "Deutschland"},
    {"was": "Fullstack Developer", "wo": "Deutschland"},
    {"was": "Backend Developer", "wo": "Deutschland"},
    {"was": "DevOps Engineer", "wo": "Deutschland"},
]

_PAGE_SIZE = 100
_MAX_PAGES = 2  # 200 per query x 5 queries = 1000 max before dedup


class StepstoneSource(BaseSource):
    """German jobs via the Arbeitsagentur public API (replaces Stepstone scraper)."""

    name = "stepstone"

    async def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_refnrs: set[str] = set()

        headers = {
            "X-API-Key": _API_KEY,
            "Accept": "application/json",
        }

        for query in _SEARCH_QUERIES:
            for page in range(1, _MAX_PAGES + 1):
                params = {
                    "was": query["was"],
                    "wo": query["wo"],
                    "arbeitszeit": "ho",  # home-office / remote
                    "umkreis": 200,       # 200km radius (approx all of Germany)
                    "size": _PAGE_SIZE,
                    "page": page,
                }

                try:
                    resp = await self._get(
                        _API_URL,
                        headers=headers,
                        params=params,
                    )
                except Exception as exc:
                    logger.error(
                        "[{}] API request failed (query='{}', page={}): {}",
                        self.name, query["was"], page, exc,
                    )
                    break

                if resp.status_code != 200:
                    logger.warning(
                        "[{}] API returned {} for query='{}'",
                        self.name, resp.status_code, query["was"],
                    )
                    break

                data = resp.json()
                postings = data.get("stellenangebote") or []
                if not postings:
                    break

                for posting in postings:
                    refnr = posting.get("refnr", "")
                    if refnr in seen_refnrs:
                        continue
                    seen_refnrs.add(refnr)

                    try:
                        job = self._parse_posting(posting)
                        if job:
                            all_jobs.append(job)
                    except (ValidationError, KeyError, TypeError) as exc:
                        logger.debug("[{}] Skipping posting: {}", self.name, exc)

                # Stop paginating if we got fewer than a full page
                if len(postings) < _PAGE_SIZE:
                    break

        logger.info("[{}] Fetched {} unique jobs from Arbeitsagentur", self.name, len(all_jobs))
        return all_jobs

    # ------------------------------------------------------------------
    # Posting -> Job
    # ------------------------------------------------------------------

    def _parse_posting(self, posting: dict) -> Job | None:
        """Convert a single API posting into a Job."""
        title = (posting.get("titel") or posting.get("beruf") or "").strip()
        if not title:
            return None

        # URL — prefer the external URL, fall back to the agency detail page
        refnr = posting.get("refnr", "")
        url = posting.get("externeUrl") or ""
        if not url and refnr:
            url = f"https://www.arbeitsagentur.de/jobsuche/suche?id={refnr}"
        if not url:
            return None

        company = (posting.get("arbeitgeber") or "Unknown").strip()

        # Location
        location = self._build_location(posting)

        # Determine remote scope
        is_remote = True  # We specifically filter for home-office
        remote_scope = "germany"

        # Posted date
        posted_at = None
        date_str = posting.get("aktuelleVeroeffentlichungsdatum") or ""
        if date_str:
            try:
                posted_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                try:
                    posted_at = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except (ValueError, TypeError):
                    pass

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=is_remote,
            remote_scope=remote_scope,
            url=url,
            description=None,
            salary=None,
            tags=[],
            source=self.name,
            posted_at=posted_at,
        )

    @staticmethod
    def _build_location(posting: dict) -> str:
        """Extract location from the nested arbeitsort structure."""
        arbeitsort = posting.get("arbeitsort") or {}
        parts: list[str] = []

        ort = arbeitsort.get("ort")
        if ort:
            parts.append(ort)

        region = arbeitsort.get("region")
        if region and region not in parts:
            parts.append(region)

        if not parts:
            land = arbeitsort.get("land")
            if land:
                parts.append(land)

        return ", ".join(parts) if parts else "Germany (Remote)"
