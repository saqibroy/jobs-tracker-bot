"""GoodJobs.eu source — httpx + BeautifulSoup scraper.

URL: https://goodjobs.eu/jobs?category=it

EU-focused, mission-driven organisations.  Good for DE/EU remote roles.
Primarily German-language but we scrape the listings and let the
language filter handle non-English content.

Method: httpx GET → parse HTML with BeautifulSoup.
The page renders job listings server-side (no JS required).

NGO classification: partial — set ``is_ngo=True`` when the company
name or description contains NGO/nonprofit signals (e.g. gGmbH, e.V.,
Stiftung).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://goodjobs.eu"
_LISTING_URL = f"{_BASE_URL}/jobs"
_LISTING_PARAMS = {"category": "it"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# German legal forms that indicate NGO/nonprofit
_NGO_LEGAL_FORMS: list[str] = [
    "ggmbh",
    "e.v.",
    "e. v.",
    "stiftung",
    "gemeinnützig",
    "gug",
    "non-profit",
    "nonprofit",
    "ngo",
]


class GoodJobsSource(BaseSource):
    name = "goodjobs"

    async def fetch(self) -> list[Job]:
        resp = await self._get(_LISTING_URL, params=_LISTING_PARAMS, headers=_HEADERS)

        if resp.status_code == 429:
            return []

        html = resp.text
        if not html or len(html) < 500:
            logger.warning("[{}] Empty or very short response", self.name)
            return []

        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        # Find all job links: they point to /jobs/<slug>
        job_links = soup.find_all("a", href=re.compile(r"^(/jobs/[a-z0-9-]+|https://goodjobs\.eu/jobs/[a-z0-9-]+)"))

        seen_urls: set[str] = set()

        for link in job_links:
            href = link.get("href", "")
            url = href if href.startswith("http") else f"{_BASE_URL}{href}"

            # Skip navigation links (pages, categories, etc.)
            if "?category=" in url or "?page=" in url or "previous-page" in url:
                continue
            # Must be a specific job slug (not just /jobs)
            if url.rstrip("/") == f"{_BASE_URL}/jobs":
                continue

            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                job = self._parse_job_link(link, url)
                if job:
                    jobs.append(job)
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                logger.debug("[{}] Skipping malformed listing: {}", self.name, exc)

        if not jobs:
            logger.warning("[{}] No jobs parsed from HTML ({} chars)", self.name, len(html))

        return jobs

    def _parse_job_link(self, link_element, url: str) -> Job | None:
        """Parse a single job from its anchor element (jobcard)."""
        full_text = link_element.get_text(" ", strip=True)
        if not full_text or len(full_text) < 10:
            return None

        # Skip non-job links
        skip_keywords = ["anmelden", "konto erstellen", "stellenanzeige", "cookie"]
        if any(kw in full_text.lower() for kw in skip_keywords):
            return None

        # ── Title from h3 heading ─────────────────────────────────────
        h3 = link_element.find("h3")
        title = h3.get_text(strip=True) if h3 else ""

        if not title or len(title) < 3:
            return None

        # ── Location from structured spans ────────────────────────────
        location = self._extract_location(full_text)

        # ── Company from the bottom of the card ──────────────────────
        company = self._extract_company_from_card(link_element, full_text)

        # ── Salary ────────────────────────────────────────────────────
        salary = self._extract_salary(full_text)

        # ── NGO status ────────────────────────────────────────────────
        is_ngo = self._is_ngo_company(company, full_text)

        # ── Tags ──────────────────────────────────────────────────────
        tags: list[str] = []
        if "hybrid" in full_text.lower():
            tags.append("Hybrid")
        if "remote" in full_text.lower():
            tags.append("Remote")
        if "vollzeit" in full_text.lower() or "full-time" in full_text.lower():
            tags.append("Full-time")
        if "teilzeit" in full_text.lower() or "part-time" in full_text.lower():
            tags.append("Part-time")

        # ── Remote status ─────────────────────────────────────────────
        text_lower = full_text.lower()
        is_remote = "remote" in text_lower or "hybrid" in text_lower

        return Job(
            title=title,
            company=company,
            location=location if location else "Germany",
            is_remote=is_remote,
            url=url,
            description=full_text[:2000],
            salary=salary,
            tags=tags[:10],
            source=self.name,
            is_ngo=is_ngo,
            posted_at=None,
        )

    @staticmethod
    def _extract_company_from_card(link_element, full_text: str) -> str:
        """Extract company name from the job card element.

        The company name is inside a ``div.grow > div.mb-1 > div > p``
        structure near the bottom of the card, following the job metadata
        and before the second "GoodCompany" section.
        """
        # The company section is a div.mb-1 inside a shallow div.grow
        mb_divs = link_element.find_all("div", class_="mb-1")
        for mb_div in mb_divs:
            # The company div.mb-1 contains a <p> with the company name
            p = mb_div.find("p")
            if p:
                text = p.get_text(strip=True)
                if (
                    text
                    and 2 < len(text) < 100
                    and "GoodCompan" not in text
                    and "Nachhaltigkeits" not in text
                    and "Auswahlprozess" not in text
                    and "bedeutet" not in text
                ):
                    return text

        return GoodJobsSource._extract_company(full_text)

    @staticmethod
    def _extract_location(text: str) -> str:
        """Extract location from job listing text."""
        # Look for German city names followed by | or pipe
        # Common pattern: "Berlin | Hybrid" or "Hamburg | Nur vor Ort"
        match = re.search(
            r"(Berlin|Hamburg|München|Munich|Frankfurt|Köln|Cologne|Stuttgart|"
            r"Leipzig|Dresden|Düsseldorf|Hannover|Bremen|Bonn|Mannheim|"
            r"Heidelberg|Freiburg|Potsdam|Tübingen|Lüneburg|Heilbronn|"
            r"Bielefeld|Dortmund|Essen|Nürnberg|Nuremberg|Wiesbaden|"
            r"[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-Za-zäöüß]+)?)"
            r"\s*\|\s*(Hybrid|Remote|Nur vor Ort|Vor Ort)",
            text,
        )
        if match:
            city = match.group(1)
            work_type = match.group(2)
            if work_type == "Remote":
                return f"{city}, Germany (Remote)"
            elif work_type == "Hybrid":
                return f"{city}, Germany (Hybrid)"
            else:
                return f"{city}, Germany"

        # Fallback: look for any known city
        known_cities = [
            "Berlin", "Hamburg", "München", "Munich", "Frankfurt", "Köln",
            "Cologne", "Stuttgart", "Leipzig", "Dresden", "Düsseldorf",
            "Hannover", "Bremen", "Bonn",
        ]
        text_lower = text.lower()
        for city in known_cities:
            if city.lower() in text_lower:
                return f"{city}, Germany"

        return "Germany"

    @staticmethod
    def _extract_company(text: str) -> str:
        """Extract company name from listing text."""
        # Company is usually before "GoodCompany" or "Company" at the end
        match = re.search(r"([A-ZÄÖÜ][^\n]{3,50}?)\s+(?:GoodCompany|Company)", text)
        if match:
            company = match.group(1).strip()
            # Clean up: remove work type prefixes
            for prefix in ["Anstellungsart: Festanstellung", "Anstellungsart:",
                           "Deutsch", "Englisch", "Zu den Ersten gehören",
                           "Kein Anschreiben benötigt"]:
                company = company.replace(prefix, "").strip()
            if company:
                return company

        return "Unknown"

    @staticmethod
    def _extract_salary(text: str) -> str | None:
        """Extract salary from listing text."""
        # Pattern: "Jahresgehalt 40.000€ – 47.000€" or "50.000€ – 65.000€"
        match = re.search(r"(?:Jahresgehalt\s+)?(\d{1,3}(?:\.\d{3})*€)\s*[–-]\s*(\d{1,3}(?:\.\d{3})*€)", text)
        if match:
            return f"{match.group(1)} – {match.group(2)}/yr"
        return None

    @staticmethod
    def _is_ngo_company(company: str, full_text: str) -> bool:
        """Determine if the company is an NGO based on name signals."""
        combined = f"{company} {full_text}".lower()
        return any(form in combined for form in _NGO_LEGAL_FORMS)
