"""ReliefWeb source — RSS feeds from reliefweb.int.

Since November 2025 the ReliefWeb JSON API requires a pre-approved appname.
We use the public RSS feeds instead, which need no authentication.

Feeds:
  ICT:  https://reliefweb.int/jobs/rss.xml?search=career_categories.exact:"Information and Communications Technology"
  PPM:  https://reliefweb.int/jobs/rss.xml?search=career_categories.exact:"Program/Project Management"
  IM:   https://reliefweb.int/jobs/rss.xml?search=career_categories.exact:"Information Management"

Each feed returns ~20 recent items with: title, link, pubDate, author (org),
summary (HTML with country, org, description), tags (country, org, category).

All ReliefWeb jobs are from humanitarian/UN organizations → is_ngo=True.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource

_BASE_RSS = "https://reliefweb.int/jobs/rss.xml"

_CATEGORY_QUERIES = {
    "ICT": "Information and Communications Technology",
    "PPM": "Program/Project Management",
    "IM": "Information Management",
}


def _rss_url(category_name: str) -> str:
    """Build the RSS URL for a career category search."""
    search = f'career_categories.exact:"{category_name}"'
    return f"{_BASE_RSS}?search={quote(search)}"


# Title keywords used to filter PPM/IM jobs that are actually tech roles.
_TECH_TITLE_KEYWORDS = [
    "software", "developer", "engineer", "digital", "data",
    "ict", "it ", "i.t.", "web ", "tech", "devops", "cloud",
    "systems", "platform", "full stack", "fullstack", "frontend",
    "backend", "machine learning", "ai ",
]


class ReliefWebSource(BaseSource):
    name = "reliefweb"

    async def fetch(self) -> list[Job]:
        # Fetch all 3 RSS feeds concurrently
        feeds = await asyncio.gather(
            self._fetch_feed("ICT"),
            self._fetch_feed("PPM"),
            self._fetch_feed("IM"),
        )

        feed_ict, feed_ppm, feed_im = feeds

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        # Parse ICT results (no title filter — all are tech)
        for entry in feed_ict:
            try:
                job = self._parse_entry(entry)
                if job is not None and job.url not in seen_urls:
                    seen_urls.add(job.url)
                    jobs.append(job)
            except (ValidationError, Exception) as exc:
                title = entry.get("title", "???")
                logger.debug("[reliefweb] Skipping malformed ICT item '{}': {}", title, exc)

        ict_count = len(jobs)

        # Parse PPM + IM — only keep jobs with tech-related titles
        extra_total = 0
        extra_kept = 0
        for label, entries in [("PPM", feed_ppm), ("IM", feed_im)]:
            for entry in entries:
                extra_total += 1
                try:
                    job = self._parse_entry(entry)
                    if job is None or job.url in seen_urls:
                        continue
                    if self._has_tech_title(job.title):
                        seen_urls.add(job.url)
                        jobs.append(job)
                        extra_kept += 1
                    else:
                        logger.debug(
                            "[reliefweb] {} skip (no tech keyword): {}", label, job.title
                        )
                except (ValidationError, Exception) as exc:
                    title = entry.get("title", "???")
                    logger.debug("[reliefweb] Skipping malformed {} item '{}': {}", label, title, exc)

        logger.info(
            "[reliefweb] Fetched {} ICT + {}/{} PPM+IM = {} total",
            ict_count, extra_kept, extra_total, len(jobs),
        )

        return jobs

    @staticmethod
    def _has_tech_title(title: str) -> bool:
        """Check if a title contains a tech-related keyword."""
        title_lower = title.lower()
        return any(kw in title_lower for kw in _TECH_TITLE_KEYWORDS)

    # ── Feed fetching ──────────────────────────────────────────────────

    async def _fetch_feed(self, label: str) -> list:
        """Fetch and parse one RSS feed. Returns list of feedparser entries."""
        category = _CATEGORY_QUERIES[label]
        url = _rss_url(category)

        try:
            resp = await self._get(url)
            if resp.status_code == 429:
                logger.warning("[reliefweb] Rate limited on {} feed", label)
                return []
            feed = feedparser.parse(resp.text)
            entries = feed.entries or []
            logger.debug("[reliefweb] {} feed: {} entries", label, len(entries))
            return entries
        except Exception as exc:
            logger.error("[reliefweb] {} feed failed: {}", label, exc)
            return []

    # ── Parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_entry(entry) -> Job | None:
        """Convert a feedparser entry into a Job."""
        title = entry.get("title", "").strip()
        if not title:
            return None

        url = entry.get("link", "") or entry.get("id", "")
        if not url:
            return None

        # Organization name from the author field
        company = entry.get("author", "Unknown") or "Unknown"

        # Publication date
        posted_at = None
        published = entry.get("published_parsed")
        if published:
            try:
                posted_at = datetime(
                    *published[:6], tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                pass

        # Location — extract country from tags or summary HTML
        location = ReliefWebSource._extract_location(entry)

        # Description from summary (HTML)
        description = entry.get("summary", "") or ""

        # Tags from feedparser tags
        tags: list[str] = []
        for tag in (entry.get("tags") or []):
            term = tag.get("term", "")
            if term:
                tags.append(term)

        return Job(
            title=title,
            company=company,
            location=location,
            is_remote=False,  # ReliefWeb jobs rarely specify remote
            remote_scope=None,
            url=url,
            description=description[:5000] if description else None,
            salary=None,
            tags=tags,
            source="reliefweb",
            is_ngo=True,  # All ReliefWeb orgs are humanitarian/UN
            posted_at=posted_at,
        )

    @staticmethod
    def _extract_location(entry) -> str:
        """Extract country from RSS entry summary HTML, falling back to tags."""
        # Primary: parse country from the summary HTML div
        summary = entry.get("summary", "")
        match = re.search(r'class="tag country">Country:\s*([^<]+)<', summary)
        if match:
            return match.group(1).strip()

        # Fallback: infer from tags — the first tag is usually the country,
        # but we must skip org names and category labels.
        author = (entry.get("author") or "").lower()
        for tag in (entry.get("tags") or []):
            term = tag.get("term", "")
            if not term or term in _CATEGORY_QUERIES.values():
                continue
            # Skip if this tag matches the author/org name
            if term.lower() == author:
                continue
            # Skip common org-name words
            if any(w in term for w in ("Organization", "Committee",
                       "Agency", "Fund", "Programme", "Office", "Council",
                       "Foundation", "Association", "Society", "Network",
                       "Coalition", "Commission", "Job")):
                continue
            return term

        return "Unspecified"
