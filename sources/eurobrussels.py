"""EuroBrussels source — httpx + BeautifulSoup scraper.

URL: https://www.eurobrussels.com/job_search
     (also checks the IT/Operations category page)

EU-focused job board, strong for NGO/policy/civil society tech roles.
Most jobs are in Brussels or other EU cities.

Method: HTTP GET → parse HTML job listings with BeautifulSoup.

NGO classification: set ``is_ngo=True`` only when the job type tag
indicates NGO, EU Institution, or International Organisation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_URL = "https://www.eurobrussels.com"

# We search the main job search and the operations (IT) category
_SEARCH_URLS = [
    f"{_BASE_URL}/job_search",
    f"{_BASE_URL}/jobs/operations_accounts_hr_it",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Job types that indicate NGO/EU institution/international org
_NGO_TYPE_KEYWORDS: set[str] = {
    "ngo and political",
    "ngo",
    "political",
    "eu institution",
    "international organisations",
    "international organization",
    "academic and think tank",
    "think tank",
}


class EuroBrusselsSource(BaseSource):
    name = "eurobrussels"

    async def fetch(self) -> list[Job]:
        seen_urls: set[str] = set()
        all_jobs: list[Job] = []

        for search_url in _SEARCH_URLS:
            try:
                resp = await self._get(search_url, headers=_HEADERS)
                if resp.status_code == 429:
                    logger.warning("[{}] Rate limited on {}", self.name, search_url)
                    continue

                jobs = self._parse_listing_page(resp.text, seen_urls)
                all_jobs.extend(jobs)
            except Exception as exc:
                logger.error("[{}] Failed to fetch {}: {}", self.name, search_url, exc)

        if not all_jobs:
            logger.warning("[{}] No jobs parsed from any page", self.name)

        return all_jobs

    def _parse_listing_page(self, html: str, seen_urls: set[str]) -> list[Job]:
        """Parse a EuroBrussels listing page and return Jobs."""
        if not html or len(html) < 500:
            logger.warning("[{}] Empty or very short response", self.name)
            return []

        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        # EuroBrussels job links follow pattern: /job_display/<id>/<slug>
        job_links = soup.find_all("a", href=re.compile(r"/job_display/\d+/"))

        # Deduplicate links — prefer the one with actual text content
        # (first link per job is usually an image/logo, second has title text)
        unique_links: dict[str, object] = {}
        for link in job_links:
            href = link.get("href", "")
            url = href if href.startswith("http") else f"{_BASE_URL}{href}"
            text = link.get_text(strip=True)
            if url not in unique_links or (text and len(text) >= 5):
                unique_links[url] = link

        for url, link in unique_links.items():
            if url in seen_urls:
                continue

            try:
                job = self._parse_job_from_link(link, url)
                if job:
                    seen_urls.add(url)
                    jobs.append(job)
            except (ValidationError, KeyError, TypeError, AttributeError) as exc:
                logger.debug("[{}] Skipping malformed listing: {}", self.name, exc)

        return jobs

    def _parse_job_from_link(self, link_element, url: str) -> Job | None:
        """Parse a single job from its link element and surrounding context."""
        # ── Title ──────────────────────────────────────────────────────
        title = link_element.get_text(strip=True)

        # Fallback: extract title from img alt attribute (logo links)
        if not title or len(title) < 5:
            img = link_element.find("img")
            if img:
                title = img.get("alt", "").strip()

        # Fallback: extract title from URL slug
        if not title or len(title) < 5:
            title = self._title_from_url(url)

        if not title or len(title) < 5:
            return None

        # Skip if it looks like a navigation link, not a job title
        if title.lower() in ("save this job", "email me jobs like this", "subscribe"):
            return None

        # ── Company + Location from surrounding context ────────────────
        # Actual HTML structure: parent div.ps-3 contains:
        #   h3 > a → title
        #   div.companyName → company
        #   div.location → city
        parent = link_element.find_parent(["div", "li", "article", "section"])

        company = "Unknown"
        location = ""
        tags: list[str] = []
        description = ""
        is_ngo = False

        if parent:
            # ── Company from div.companyName ──────────────────────────
            company_el = parent.find("div", class_="companyName")
            if company_el:
                company = company_el.get_text(strip=True) or "Unknown"

            # ── Location from div.location ────────────────────────────
            loc_el = parent.find("div", class_="location")
            if loc_el:
                location = loc_el.get_text(strip=True)

            # Fallback: extract from URL slug if not found in HTML
            if company == "Unknown" or not location:
                url_company, url_location = self._parse_url_metadata(url)
                if company == "Unknown" and url_company != "Unknown":
                    company = url_company
                if not location:
                    location = url_location

            # Walk up to find the larger job card container for tags
            card_parent = parent.find_parent(["div", "article", "section"])
            tag_container = card_parent if card_parent else parent

            # Extract category tags and NGO status
            tags, is_ngo = self._extract_tags_and_ngo_status(tag_container)

            # Description snippet
            desc_parts = []
            for p in tag_container.find_all(["p", "span"]):
                text = p.get_text(strip=True)
                if text and len(text) > 30 and text != title:
                    desc_parts.append(text)
            description = " ".join(desc_parts[:3])
        else:
            _, location = self._parse_url_metadata(url)

        if not location:
            location = "Brussels, Belgium"  # Default for EuroBrussels

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=False,  # Most EuroBrussels jobs don't specify remote
            url=url,
            description=description[:5000] if description else None,
            salary=None,
            tags=tags[:10],
            source=self.name,
            is_ngo=is_ngo,
            posted_at=None,
        )

    @staticmethod
    def _parse_url_metadata(url: str) -> tuple[str, str]:
        """Extract company and location from the EuroBrussels URL slug.

        URL format: /job_display/288443/Title_Company_City_Country
        Example: /job_display/288443/Coordinator_Energy_Transition_Operations_EDF_Environmental_Defense_Fund_Brussels_Belgium
        """
        company = "Unknown"
        location = ""

        match = re.search(r"/job_display/\d+/(.+)$", url)
        if not match:
            return company, location

        slug = match.group(1)
        parts = slug.split("_")

        # Try to find city names at the end of the slug
        known_cities = {
            "Brussels", "Bonn", "Berlin", "Paris", "Luxembourg",
            "Amsterdam", "Geneva", "The", "Prague", "Vienna",
            "Dublin", "Madrid", "Rome", "Grenoble", "Strasbourg",
            "Stockholm", "Copenhagen", "Helsinki", "Lisbon", "Warsaw",
        }
        known_countries = {
            "Belgium", "Germany", "France", "Luxembourg", "Netherlands",
            "Czech", "Republic", "Austria", "Ireland", "Spain", "Italy",
            "Sweden", "Denmark", "Norway", "Finland", "Poland",
            "Portugal", "Greece", "Europe", "Outside",
        }

        # Walk backwards to find city/country
        city_parts: list[str] = []
        remaining_parts = list(parts)

        while remaining_parts:
            last = remaining_parts[-1]
            if last in known_countries or last in known_cities:
                city_parts.insert(0, last)
                remaining_parts.pop()
            else:
                break

        if city_parts:
            location = " ".join(city_parts)
            # Replace "Outside Europe" with the actual location if present
            if "Outside" in location:
                location = " ".join(city_parts)

        return company, location

    @staticmethod
    def _extract_tags_and_ngo_status(parent_element) -> tuple[list[str], bool]:
        """Extract category tags from the parent element and determine NGO status."""
        tags: list[str] = []
        is_ngo = False

        # Get text from spans / small elements that look like tags
        full_text = parent_element.get_text(" ", strip=True).lower()

        # Check known category keywords in the text
        category_keywords = [
            "Communication/Public Relations",
            "EU Institution",
            "Industry Association",
            "International Organisations",
            "NGO and Political",
            "Academic and Think Tank",
            "Consultancy",
            "Economist",
            "Legal",
            "Operations (Accounts, HR, IT)",
            "Secretarial and Assistant",
        ]

        for cat in category_keywords:
            if cat.lower() in full_text:
                tags.append(cat)

        # Determine NGO status
        for ngo_keyword in _NGO_TYPE_KEYWORDS:
            if ngo_keyword in full_text:
                is_ngo = True
                break

        # Check for "Hybrid" or "Remote" workplace type
        if "hybrid" in full_text:
            tags.append("Hybrid")
        if "remote" in full_text:
            tags.append("Remote")

        return tags, is_ngo

    @staticmethod
    def _title_from_url(url: str) -> str:
        """Extract a human-readable title from a EuroBrussels URL slug.

        URL format: /job_display/ID/Title_Part_Company_City_Country
        We take the slug and convert underscores to spaces, then return
        a best-effort title (first few words before the company name).
        """
        match = re.search(r"/job_display/\d+/(.+)$", url)
        if not match:
            return ""
        slug = match.group(1)
        # Replace underscores with spaces
        return slug.replace("_", " ")
