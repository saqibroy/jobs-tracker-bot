"""RemoteOK source — free JSON API.

Endpoint: https://remoteok.com/api
Returns a JSON array. The first element is metadata (last_updated, legal).
Remaining elements are job objects.

Fields per job:
  slug, id, epoch, date (ISO 8601), company, company_logo, position,
  tags[], description (HTML), location, apply_url, url,
  salary_min, salary_max, original (bool)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://remoteok.com/api"

# ── RemoteOK location pre-parsing ─────────────────────────────────────────
# Map raw location strings to (remote_scope, normalized_location).
# RemoteOK is a remote-only board, so bare "Remote" → worldwide.
_REMOTEOK_WORLDWIDE_PATTERNS: list[str] = [
    "worldwide", "global", "anywhere", "work from anywhere",
    "remote - worldwide", "remote worldwide",
]

_REMOTEOK_EU_PATTERNS: list[str] = [
    "europe", "eu", "emea", "remote - europe", "remote europe",
    "remote - eu", "remote eu",
]

_REMOTEOK_GERMANY_PATTERNS: list[str] = [
    "germany", "deutschland", "berlin", "munich", "münchen",
    "hamburg", "frankfurt",
]

# Countries / regions that indicate non-EU restriction
_REMOTEOK_RESTRICTED_PATTERNS: list[str] = [
    "united states", "usa", "us", "us only", "remote us", "remote - us",
    "remote, us", "u.s.",
    "canada", "canada only", "remote canada", "remote - canada",
    "australia", "new zealand", "brazil", "india", "nigeria",
    "singapore", "japan", "south korea", "china",
    "united kingdom", "uk", "uk only", "england", "london",
    "mexico", "argentina", "colombia",
    "americas", "apac", "latam", "latin america",
    # US states
    "california", "new york", "texas", "florida", "illinois",
    "massachusetts", "washington", "colorado", "georgia", "virginia",
    "north carolina", "pennsylvania", "ohio", "michigan", "arizona",
    "oregon", "minnesota", "tennessee", "maryland", "connecticut",
    # US / Canadian cities
    "san francisco", "los angeles", "nyc", "seattle", "austin",
    "boston", "chicago", "denver", "atlanta", "miami", "portland",
    "dallas", "houston", "tampa", "toronto", "vancouver", "montreal",
]

# Short tokens that need word-boundary matching in RemoteOK parser
_REMOTEOK_SHORT_TOKENS: set[str] = {"us", "uk", "usa"}


def _parse_remoteok_location(raw_location: str) -> tuple[str, str | None]:
    """Parse a RemoteOK location string into (normalized_location, remote_scope).

    Returns (location_string, scope) where scope is one of:
    - "worldwide", "eu", "germany", "restricted", or None (let classifier decide).
    """
    loc = raw_location.strip().lower()

    # Empty or bare "Remote" → worldwide (RemoteOK is remote-only)
    if not loc or loc == "remote":
        return (raw_location or "Remote", "worldwide")

    # Check for worldwide signals
    for pattern in _REMOTEOK_WORLDWIDE_PATTERNS:
        if pattern in loc:
            return (raw_location, "worldwide")

    # Check for Germany signals
    for pattern in _REMOTEOK_GERMANY_PATTERNS:
        if pattern in loc:
            return (raw_location, "germany")

    # Check for EU signals
    for pattern in _REMOTEOK_EU_PATTERNS:
        if pattern in loc:
            return (raw_location, "eu")

    # Check for restricted patterns (non-EU countries)
    # But first check if it ALSO mentions an EU country or worldwide
    from filters.location import _EU_COUNTRIES, _WORLDWIDE_KEYWORDS

    has_eu = any(c in loc for c in _EU_COUNTRIES)
    has_worldwide = any(w in loc for w in _WORLDWIDE_KEYWORDS)

    if not has_eu and not has_worldwide:
        for pattern in _REMOTEOK_RESTRICTED_PATTERNS:
            # Word-boundary match for short tokens
            if pattern in _REMOTEOK_SHORT_TOKENS or len(pattern) <= 3:
                if re.search(rf"\b{re.escape(pattern)}\b", loc):
                    return (raw_location, "restricted")
            else:
                if pattern in loc:
                    return (raw_location, "restricted")

    # Couldn't determine — let the main classifier handle it
    # For RemoteOK (remote-only board), if we can't determine, it's likely worldwide
    return (raw_location, "worldwide")


class RemoteOKSource(BaseSource):
    name = "remoteok"

    async def fetch(self) -> list[Job]:
        # RemoteOK sometimes blocks non-browser User-Agents
        resp = await self._get(
            _API_URL,
            headers={"User-Agent": "job-tracker-bot/1.0"},
        )

        if resp.status_code == 429:
            return []

        data = resp.json()

        # First element is metadata — skip it
        if not isinstance(data, list) or len(data) < 2:
            logger.warning("[{}] Unexpected response format", self.name)
            return []

        raw_jobs = data[1:]  # skip metadata element
        jobs: list[Job] = []

        for item in raw_jobs:
            try:
                # Skip if it looks like metadata rather than a job
                if not isinstance(item, dict) or "position" not in item:
                    continue

                # Parse publication date
                posted_at = None
                date_str = item.get("date")
                if date_str:
                    try:
                        posted_at = datetime.fromisoformat(
                            str(date_str).replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass

                # Fall back to epoch timestamp
                if not posted_at and item.get("epoch"):
                    try:
                        posted_at = datetime.fromtimestamp(
                            int(item["epoch"]), tz=timezone.utc
                        )
                    except (ValueError, TypeError, OSError):
                        pass

                # Tags
                tags = item.get("tags", []) or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

                # Location — often empty on RemoteOK
                location = item.get("location", "") or ""
                if not location:
                    location = "Remote"

                # Pre-parse RemoteOK location to set scope
                location, remoteok_scope = _parse_remoteok_location(location)

                # Salary — combine min/max if available
                salary = None
                sal_min = item.get("salary_min")
                sal_max = item.get("salary_max")
                if sal_min and sal_max and int(sal_min) > 0 and int(sal_max) > 0:
                    salary = f"${int(sal_min):,} – ${int(sal_max):,}"
                elif sal_min and int(sal_min) > 0:
                    salary = f"${int(sal_min):,}+"
                elif sal_max and int(sal_max) > 0:
                    salary = f"Up to ${int(sal_max):,}"

                # URL — prefer the apply URL, fall back to RemoteOK listing
                url = item.get("url", "") or item.get("apply_url", "")
                if not url:
                    slug = item.get("slug", "")
                    if slug:
                        url = f"https://remoteok.com/remote-jobs/{slug}"

                if not url:
                    continue  # can't create a Job without a URL

                job = Job(
                    title=item.get("position", ""),
                    company=item.get("company", "Unknown"),
                    location=location,
                    is_remote=True,  # RemoteOK is a remote-only board
                    remote_scope=remoteok_scope,
                    url=url,
                    description=item.get("description", ""),
                    salary=salary,
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                )
                jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry: {}", self.name, exc)
                continue

        return jobs
