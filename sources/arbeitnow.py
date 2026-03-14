"""Arbeitnow.com source — free JSON API.

Endpoint: https://www.arbeitnow.com/api/job-board-api
Strong for DE/EU remote jobs. Returns JSON with a "data" array.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_API_URL = "https://www.arbeitnow.com/api/job-board-api"

# ── German cities for location parsing ─────────────────────────────────────
_KNOWN_GERMAN_CITIES: set[str] = {
    "berlin", "münchen", "munich", "hamburg", "frankfurt", "köln",
    "cologne", "düsseldorf", "stuttgart", "leipzig", "dresden",
    "hannover", "nuremberg", "nürnberg", "dortmund", "essen", "bremen",
    "bonn", "mannheim", "karlsruhe", "augsburg", "wiesbaden",
    "freiburg", "mainz", "heidelberg", "potsdam", "rostock",
}

# EU country names for location parsing
_COUNTRY_MAP: dict[str, str] = {
    "germany": "Germany", "deutschland": "Germany",
    "austria": "Austria", "österreich": "Austria",
    "switzerland": "Switzerland", "schweiz": "Switzerland",
    "france": "France", "spain": "Spain", "portugal": "Portugal",
    "italy": "Italy", "netherlands": "Netherlands",
    "belgium": "Belgium", "luxembourg": "Luxembourg",
    "ireland": "Ireland", "sweden": "Sweden", "denmark": "Denmark",
    "norway": "Norway", "finland": "Finland", "poland": "Poland",
    "czech republic": "Czech Republic", "czechia": "Czech Republic",
    "romania": "Romania", "hungary": "Hungary",
}


def _parse_arbeitnow_location(raw_location: str) -> tuple[str | None, str | None, str | None]:
    """Parse arbeitnow location string into (city, postal_code, country).

    Examples:
      "Berlin" → ("Berlin", None, "Germany")
      "13086 Berlin" → ("Berlin", "13086", "Germany")
      "Hamburg, Germany" → ("Hamburg", None, "Germany")
      "13086 Berlin, Germany" → ("Berlin", "13086", "Germany")
      "Remote" → (None, None, None)
    """
    if not raw_location:
        return None, None, None

    loc = raw_location.strip()
    city = None
    postal = None
    country = None

    # Try to extract postal code (German postal codes are 5 digits)
    postal_match = re.match(r"^(\d{4,5})\s+(.+)", loc)
    if postal_match:
        postal = postal_match.group(1)
        loc = postal_match.group(2).strip()

    # Split on comma to separate city and country parts
    parts = [p.strip() for p in loc.split(",")]

    for part in parts:
        part_lower = part.lower()
        # Check if it's a known country
        if part_lower in _COUNTRY_MAP:
            country = _COUNTRY_MAP[part_lower]
        # Check if it's a known German city
        elif part_lower in _KNOWN_GERMAN_CITIES:
            city = part
            if not country:
                country = "Germany"
        # If not recognized but looks like a city name (not "Remote", "Worldwide", etc.)
        elif part_lower not in ("remote", "worldwide", "hybrid", "on-site", "onsite"):
            if not city:
                city = part

    # If we found a German city but no country, default to Germany
    if city and city.lower() in _KNOWN_GERMAN_CITIES and not country:
        country = "Germany"

    return city, postal, country


class ArbeitnowSource(BaseSource):
    name = "arbeitnow"

    async def fetch(self) -> list[Job]:
        resp = await self._get(_API_URL)

        if resp.status_code == 429:
            return []

        data = resp.json()
        raw_jobs = data.get("data", [])
        jobs: list[Job] = []

        for item in raw_jobs:
            try:
                # Parse the created_at timestamp (unix epoch)
                posted_at = None
                created_at = item.get("created_at")
                if created_at:
                    try:
                        if isinstance(created_at, (int, float)):
                            posted_at = datetime.fromtimestamp(
                                created_at, tz=timezone.utc
                            )
                        else:
                            posted_at = datetime.fromisoformat(
                                str(created_at).replace("Z", "+00:00")
                            )
                    except (ValueError, TypeError, OSError):
                        pass

                # Build tags from the API's tags array
                tags = item.get("tags", []) or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

                # Determine remote status
                remote = item.get("remote", False)
                location = item.get("location", "")

                # Parse city/postal/country from location
                city, postal, parsed_country = _parse_arbeitnow_location(location)

                job = Job(
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    location=location,
                    is_remote=bool(remote),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    salary=item.get("salary", None) or None,
                    tags=tags,
                    source=self.name,
                    posted_at=posted_at,
                    company_city=city,
                    company_postal_code=postal,
                    company_country=parsed_country,
                )
                jobs.append(job)
            except (ValidationError, KeyError, TypeError) as exc:
                logger.warning("[{}] Skipping malformed entry: {}", self.name, exc)
                continue

        return jobs
