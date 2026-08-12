"""BerlinStartupJobs source using the anonymous WordPress REST API."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from filters.employment import classify_employment
from models.job import Job
from models.scan import SourceComponentError, SourceStatus, sanitize_source_error
from sources.base import BaseSource


_BASE_URL = "https://berlinstartupjobs.com"
_CATEGORY_URL = f"{_BASE_URL}/wp-json/wp/v2/categories"
_POSTS_URL = f"{_BASE_URL}/wp-json/wp/v2/posts"
_ENGINEERING_SLUG = "engineering"
_POSTS_PER_PAGE = 100
_MAX_POST_PAGES = 2
_MAX_DESCRIPTION_CHARS = 25_000
_MAX_TAGS = 50
_MAX_TERM_CHARS = 120

_HEADERS = {
    "User-Agent": (
        "job-tracker-bot/1.0 "
        "(BerlinStartupJobs public REST source; "
        "+https://github.com/saqibroy/jobs-tracker-bot)"
    ),
    "Accept": "application/json",
}

_POST_FIELDS = ",".join(
    (
        "id",
        "date",
        "date_gmt",
        "modified",
        "modified_gmt",
        "link",
        "slug",
        "title",
        "content",
        "categories",
        "tags",
        "job_company",
        "job_location",
        "job_plan",
        "_links",
        "_embedded",
    )
)

_SPACE_RE = re.compile(r"\s+")
_SALARY_PATTERNS = (
    re.compile(
        r"(?i)(?:salary|gehalt|compensation|vergütung|pay range)\s*[:\-]?\s*"
        r"(?:€\s*)?\d[\d.,]*(?:\s*[–—-]\s*(?:€\s*)?\d[\d.,]*)?"
        r"(?:\s*(?:EUR|€|k))?(?:\s*(?:p\.?a\.?|per year|yearly|annual|/year|/yr|/h|hourly))?"
    ),
    re.compile(
        r"(?i)(?:€\s*\d[\d.,]*|\d[\d.,]*\s*(?:EUR|€))"
        r"(?:\s*[–—-]\s*(?:€\s*)?\d[\d.,]*(?:\s*(?:EUR|€))?)?"
        r"(?:\s*(?:p\.?a\.?|per year|yearly|annual|/year|/yr|/h|hourly))?"
    ),
)


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))
    return _SPACE_RE.sub(" ", text).strip()[:limit]


def _parse_wordpress_datetime(item: dict) -> datetime | None:
    for field in ("date_gmt", "date"):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _canonical_post_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "berlinstartupjobs.com"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.strip("/")
        or parsed.path.startswith("/wp-")
    ):
        return None
    return urlunsplit(("https", "berlinstartupjobs.com", parsed.path, "", ""))


def _embedded_terms(item: dict, taxonomy: str) -> list[str]:
    embedded = item.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    groups = embedded.get("wp:term")
    if not isinstance(groups, list):
        return []

    values: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for term in group:
            if not isinstance(term, dict) or term.get("taxonomy") != taxonomy:
                continue
            name = _bounded_text(term.get("name"), _MAX_TERM_CHARS)
            if name and name not in values:
                values.append(name)
    return values


def _extract_salary(description: str, tags: list[str]) -> str | None:
    evidence = f"{' '.join(tags)} {description}"
    for pattern in _SALARY_PATTERNS:
        match = pattern.search(evidence)
        if match:
            return _SPACE_RE.sub(" ", match.group(0)).strip()[:120]
    return None


def _workplace_mapping(locations: list[str]) -> tuple[str, bool, str | None, list[str]]:
    lowered = {value.casefold() for value in locations}
    has_berlin = "berlin, germany" in lowered
    has_remote_possible = "remote possible" in lowered
    has_remote = "remote" in lowered

    if has_remote_possible:
        return "hybrid", False, None, []
    if has_remote:
        if has_berlin:
            return "remote", True, "germany", ["de"]
        return "remote", True, "unknown", []
    if has_berlin:
        return "onsite", False, None, []
    return "unknown", False, None, []


class BerlinStartupJobsSource(BaseSource):
    """Fetch the bounded IT/software category without job-detail requests."""

    name = "berlinstartupjobs"

    async def fetch(self) -> list[Job]:
        category_id = await self._resolve_engineering_category()
        jobs: list[Job] = []
        seen_post_ids: set[int] = set()
        reported_pages: int | None = None

        for page in range(1, _MAX_POST_PAGES + 1):
            component = f"posts:page={page}"
            try:
                response = await self._get(
                    _POSTS_URL,
                    params={
                        "categories": str(category_id),
                        "per_page": str(_POSTS_PER_PAGE),
                        "page": str(page),
                        "orderby": "date",
                        "order": "desc",
                        "_embed": "wp:term",
                        "_fields": _POST_FIELDS,
                    },
                    headers=_HEADERS,
                )
                self._require_component_response(response)
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("WordPress posts response is not a list")
                total_pages = self._total_pages(response)
            except Exception as exc:
                self._record_component_issue(component, exc)
                logger.warning(
                    "[{}] Posts page {} failed: {}",
                    self.name,
                    page,
                    sanitize_source_error(exc),
                )
                break

            self._record_component_success()
            reported_pages = max(reported_pages or 0, total_pages)
            for item in payload:
                try:
                    job, post_id = self._parse_post(item)
                except (ValidationError, KeyError, TypeError, ValueError) as exc:
                    logger.debug(
                        "[{}] Skipping malformed WordPress post: {}",
                        self.name,
                        sanitize_source_error(exc),
                    )
                    continue
                if post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)
                jobs.append(job)

            del payload, response
            if total_pages <= page:
                break

        if reported_pages is not None and reported_pages > _MAX_POST_PAGES:
            self._record_component_issue(
                "posts:pagination_bound",
                SourceComponentError(
                    SourceStatus.UNKNOWN_ERROR,
                    f"WordPress reported {reported_pages} pages; hard bound is {_MAX_POST_PAGES}",
                ),
            )

        logger.info(
            "[{}] Fetched {} unique engineering jobs from at most {} pages",
            self.name,
            len(jobs),
            _MAX_POST_PAGES,
        )
        return jobs

    async def _resolve_engineering_category(self) -> int:
        response = await self._get(
            _CATEGORY_URL,
            params={
                "slug": _ENGINEERING_SLUG,
                "per_page": "1",
                "_fields": "id,slug,name,count",
            },
            headers=_HEADERS,
        )
        self._require_component_response(response)
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("WordPress category response is not a list")
        for item in payload:
            if not isinstance(item, dict) or item.get("slug") != _ENGINEERING_SLUG:
                continue
            category_id = item.get("id")
            if isinstance(category_id, int) and not isinstance(category_id, bool) and category_id > 0:
                return category_id
        raise ValueError("WordPress engineering category is unavailable")

    @staticmethod
    def _total_pages(response: httpx.Response) -> int:
        value = response.headers.get("X-WP-TotalPages")
        try:
            pages = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("WordPress pagination header is missing or invalid") from exc
        if pages < 0:
            raise ValueError("WordPress pagination header is invalid")
        return pages

    def _parse_post(self, item: object) -> tuple[Job, int]:
        if not isinstance(item, dict):
            raise TypeError("WordPress post is not an object")
        post_id = item.get("id")
        if not isinstance(post_id, int) or isinstance(post_id, bool) or post_id <= 0:
            raise ValueError("WordPress post ID is missing or invalid")

        title_value = item.get("title")
        content_value = item.get("content")
        title = _bounded_text(
            title_value.get("rendered") if isinstance(title_value, dict) else None,
            200,
        )
        description = _bounded_text(
            content_value.get("rendered") if isinstance(content_value, dict) else None,
            _MAX_DESCRIPTION_CHARS,
        )
        companies = _embedded_terms(item, "job_company")
        locations = _embedded_terms(item, "job_location")
        categories = _embedded_terms(item, "category")
        post_tags = _embedded_terms(item, "post_tag")
        plan_tags = _embedded_terms(item, "job_plan")
        url = _canonical_post_url(item.get("link"))

        if not title or not companies or not locations or url is None:
            raise ValueError("WordPress post is missing required normalized fields")

        tags = (categories + post_tags + plan_tags)[:_MAX_TAGS]
        workplace_type, is_remote, remote_scope, eligible_countries = _workplace_mapping(
            locations
        )
        job = Job(
            id=f"{self.name}:{post_id}",
            title=title,
            company=" / ".join(companies),
            location="; ".join(locations),
            is_remote=is_remote,
            workplace_type=workplace_type,  # type: ignore[arg-type]
            eligible_countries=eligible_countries,
            remote_scope=remote_scope,
            url=url,
            description=description or None,
            salary=_extract_salary(description, tags),
            tags=tags,
            source=self.name,
            posted_at=_parse_wordpress_datetime(item),
        )
        return classify_employment(job), post_id
