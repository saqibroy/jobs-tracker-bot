"""Idealist.org source — Algolia search API.

Idealist uses Algolia for search.  The public search-only credentials
are served in ``window.Idealist.config`` on every page load:

  App ID  : NSV3AUESS7
  API Key : c2730ea10ab82787f2f3cc961e8c1e06  (search-only, safe to embed)
  Index   : idealist7-production

We POST to the Algolia REST API to fetch remote NGO/nonprofit job
listings.  No browser rendering required.

We run two queries to maximise tech-role coverage:
  1. ``functions:TECHNOLOGY_IT`` — catches all IT-tagged listings (~36)
  2. ``query="software engineer"`` — catches dev roles without the tag

Results are deduplicated by ``objectID`` before returning.

``remoteZone`` mapping (Algolia values → our ``remote_scope``):
  WORLD                       → worldwide
  COUNTRY + EU-allowlisted    → eu
  COUNTRY + non-EU            → restricted  (country-locked, not EU)
  STATE / CITY                → restricted  (geo-locked to a region)
  <missing/empty>             → worldwide   (Idealist default)

Fields per hit:
  name, orgName, type, locationType, description (plain text),
  url {en: "/en/…"}, published (unix epoch), keywords[],
  salaryCurrency, salaryMinimum, salaryMaximum, salaryPeriod,
  remoteOk, city, state, country, orgType, areasOfFocus[], functions[]
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_ALGOLIA_APP_ID = "NSV3AUESS7"
_ALGOLIA_API_KEY = "c2730ea10ab82787f2f3cc961e8c1e06"
_ALGOLIA_INDEX = "idealist7-production"
_ALGOLIA_URL = (
    f"https://{_ALGOLIA_APP_ID}-dsn.algolia.net"
    f"/1/indexes/{_ALGOLIA_INDEX}/query"
)
_BASE_URL = "https://www.idealist.org"

# Number of results per Algolia request (max 1000)
_HITS_PER_PAGE = 50

# Two-letter ISO country codes we consider EU-eligible
_EU_COUNTRY_CODES: set[str] = {
    "DE", "AT", "CH",  # DACH
    "FR", "ES", "PT", "IT", "NL", "BE", "LU", "IE",  # Western
    "SE", "DK", "NO", "FI", "IS",  # Nordics
    "PL", "CZ", "RO", "HU", "SK", "SI", "HR", "BG",  # Central/Eastern
    "EE", "LV", "LT",  # Baltics
    "GR", "CY", "MT",  # Southern
}

# Algolia queries: (text_query, extra_filters)
_QUERIES: list[tuple[str, str]] = [
    # 1. All IT-tagged remote jobs (regardless of title)
    ("", "type:JOB AND locationType:REMOTE AND functions:TECHNOLOGY_IT"),
    # 2. Free-text "software engineer" to catch un-tagged dev roles
    ("software engineer", "type:JOB AND locationType:REMOTE"),
]


class IdealistSource(BaseSource):
    name = "idealist"

    async def fetch(self) -> list[Job]:
        # Run all Algolia queries concurrently and merge results
        tasks = [
            self._post_algolia(query=q, filters=f) for q, f in _QUERIES
        ]
        responses = await asyncio.gather(*tasks)

        seen_ids: set[str] = set()
        jobs: list[Job] = []

        for resp in responses:
            if resp is None:
                continue
            data = resp.json()
            for hit in data.get("hits", []):
                oid = hit.get("objectID", "")
                if oid in seen_ids:
                    continue  # dedup across queries
                seen_ids.add(oid)
                try:
                    job = self._parse_hit(hit)
                    if job:
                        jobs.append(job)
                except (ValidationError, KeyError, TypeError) as exc:
                    logger.debug(
                        "[{}] Skipping malformed hit {}: {}",
                        self.name,
                        oid,
                        exc,
                    )

        if not jobs:
            logger.warning("[{}] No jobs parsed from Algolia", self.name)
        return jobs

    # ── Algolia query ───────────────────────────────────────────────────
    async def _post_algolia(self, *, query: str, filters: str):
        """POST a search query to the Algolia REST API."""
        import httpx

        payload = {
            "query": query,
            "filters": filters,
            "hitsPerPage": _HITS_PER_PAGE,
            "attributesToRetrieve": [
                "name",
                "orgName",
                "orgType",
                "type",
                "locationType",
                "description",
                "url",
                "published",
                "keywords",
                "areasOfFocus",
                "functions",
                "salaryCurrency",
                "salaryMinimum",
                "salaryMaximum",
                "salaryPeriod",
                "remoteOk",
                "remoteCountry",
                "remoteZone",
                "city",
                "state",
                "stateStr",
                "country",
                "objectID",
            ],
        }

        headers = {
            "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
            "X-Algolia-API-Key": _ALGOLIA_API_KEY,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _ALGOLIA_URL, json=payload, headers=headers
                )
                if resp.status_code == 429:
                    logger.warning(
                        "[{}] Algolia rate limited (429) — skipping", self.name
                    )
                    return None
                resp.raise_for_status()
                return resp
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("[{}] Algolia request failed: {}", self.name, exc)
            return None

    # ── Parse a single Algolia hit → Job ────────────────────────────────
    def _parse_hit(self, hit: dict) -> Job | None:
        name = (hit.get("name") or "").strip()
        org = (hit.get("orgName") or "").strip()
        if not name or not org:
            return None

        # Build URL
        url_map = hit.get("url") or {}
        path = url_map.get("en", "") if isinstance(url_map, dict) else ""
        if not path:
            return None
        url = f"{_BASE_URL}{path}"

        # Location string
        location = self._build_location(hit)

        # Publication date from unix epoch
        posted_at = None
        published = hit.get("published")
        if published:
            try:
                posted_at = datetime.fromtimestamp(int(published), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                pass

        # Tags — merge keywords, areasOfFocus, functions
        tags: list[str] = []
        for key in ("keywords", "areasOfFocus", "functions"):
            vals = hit.get(key) or []
            if isinstance(vals, list):
                tags.extend(str(v).strip() for v in vals if v)

        # Salary
        salary = self._build_salary(hit)

        # Description (plain text, may be very long)
        description = hit.get("description", "") or ""

        # Determine NGO status from orgType
        org_type = (hit.get("orgType") or "").upper()
        is_ngo = org_type in {
            "NONPROFIT",
            "NGO",
            "CHARITY",
            "FOUNDATION",
            "SOCIAL_ENTERPRISE",
        }

        return Job(
            title=name,
            company=org,
            location=location,
            is_remote=True,
            remote_scope=self._classify_remote_scope(hit),
            url=url,
            description=description[:5000] if description else None,
            salary=salary,
            tags=tags,
            source=self.name,
            is_ngo=is_ngo,
            posted_at=posted_at,
        )

    # ── Helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _classify_remote_scope(hit: dict) -> str:
        """Map Algolia remoteZone / remoteCountry to our remote_scope.

        Idealist's ``locationType: REMOTE`` already guarantees the job is
        remote, so we only decide the scope label here.

        Mapping:
          WORLD                      → "worldwide"
          COUNTRY + EU country code  → "eu"
          COUNTRY + non-EU code      → "restricted"  (country-locked)
          STATE / CITY               → "restricted"  (geo-locked)
          <missing/empty>            → "worldwide"   (Idealist default)
        """
        zone = (hit.get("remoteZone") or "").upper()
        country_code = (hit.get("remoteCountry") or "").upper()

        if zone == "WORLD":
            return "worldwide"

        if zone == "COUNTRY" and country_code in _EU_COUNTRY_CODES:
            return "eu"

        if zone in ("COUNTRY", "STATE", "CITY"):
            return "restricted"

        # Missing / empty zone — default to worldwide
        return "worldwide"

    @staticmethod
    def _build_location(hit: dict) -> str:
        """Build a human-readable location string."""
        parts: list[str] = []
        city = hit.get("city")
        state = hit.get("stateStr") or hit.get("state")
        country = hit.get("country")
        if city:
            parts.append(str(city))
        if state:
            parts.append(str(state))
        if country:
            parts.append(str(country))

        base = ", ".join(parts) if parts else ""

        remote_zone = hit.get("remoteZone") or ""
        remote_country = hit.get("remoteCountry") or ""

        if remote_zone == "WORLDWIDE":
            qualifier = "Remote (Worldwide)"
        elif remote_country:
            qualifier = f"Remote ({remote_country})"
        elif remote_zone:
            qualifier = f"Remote ({remote_zone})"
        else:
            qualifier = "Remote"

        return f"{base} · {qualifier}" if base else qualifier

    @staticmethod
    def _build_salary(hit: dict) -> str | None:
        """Format salary from Algolia fields."""
        currency = hit.get("salaryCurrency") or "USD"
        sal_min = hit.get("salaryMinimum")
        sal_max = hit.get("salaryMaximum")
        period = (hit.get("salaryPeriod") or "").lower()

        period_label = {
            "year": "/yr",
            "month": "/mo",
            "week": "/wk",
            "hour": "/hr",
            "day": "/day",
        }.get(period, "")

        try:
            lo = float(sal_min) if sal_min else 0
            hi = float(sal_max) if sal_max else 0
        except (ValueError, TypeError):
            return None

        sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")

        if lo > 0 and hi > 0:
            return f"{sym}{lo:,.0f} – {sym}{hi:,.0f}{period_label}"
        if lo > 0:
            return f"{sym}{lo:,.0f}+{period_label}"
        if hi > 0:
            return f"Up to {sym}{hi:,.0f}{period_label}"
        return None
