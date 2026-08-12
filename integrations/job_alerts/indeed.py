"""Offline parser for sanitized, user-owned Indeed recommendation structures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from bs4 import Tag

from integrations.job_alerts.contracts import (
    AlertMatch,
    AlertParseStatus,
    BoundedMailContent,
    JobAlertItem,
    JobAlertParseResult,
    MailMessageMetadata,
)
from integrations.job_alerts.provider_parsing import (
    associated_card,
    associated_direct_url,
    content_soup,
    explicit_datetime,
    first_text,
    workplace_evidence,
)
from integrations.job_alerts.urls import canonicalize_indeed_job_url
from integrations.job_alerts.urls import alert_content_hash

_SENDER_RE = re.compile(r"(?:^|<)donotreply@match\.indeed\.com(?:>|$)", re.I)
_VIEW_RE = re.compile(r"\bjob anzeigen\b", re.I)
_DISMISS_RE = re.compile(r"\bpasst nicht\b", re.I)
_LIFECYCLE_RE = re.compile(
    r"\b(your application|application (?:received|update|status)|"
    r"applied to|deine bewerbung|ihre bewerbung|bewerbung (?:eingegangen|status)|"
    r"your interview|recruiter (?:message|outreach)|nachricht von)\b",
    re.I,
)
_FIELD_SELECTORS = (
    ".job-title",
    "[data-job-title]",
    ".job-company",
    "[data-company]",
    ".job-location",
    "[data-location]",
)
_SUBJECT_RE = re.compile(r"^(?P<title>.+?)\s+bei\s+(?P<company>.+)$", re.I)
_NON_LOCATION_RE = re.compile(
    r"\b(job anzeigen|passt nicht|hybrides arbeiten|hybrid|remote|homeoffice|"
    r"vollzeit|teilzeit|festanstellung|befristet)\b|(?:€|\bEUR\b)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class IndeedAlertParser:
    provider: str = "indeed"

    def matches(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch:
        text = content.cleaned_text
        sender = bool(_SENDER_RE.search(message.sender.strip()))
        view = bool(_VIEW_RE.search(text))
        dismiss = bool(_DISMISS_RE.search(text))
        has_job_link = any(_is_indeed_candidate(link) for link in content.links)
        excluded = bool(_LIFECYCLE_RE.search(" ".join((message.subject, text))))
        matched = sender and view and dismiss and has_job_link and not excluded
        evidence = tuple(
            code
            for code, present in (
                ("indeed_match_sender", sender),
                ("indeed_view_cta", view),
                ("indeed_dismiss_cta", dismiss),
                ("indeed_offline_job_link", has_job_link),
            )
            if present
        )
        return AlertMatch(
            self.provider,
            matched,
            confidence=98 if matched else 0,
            evidence=evidence,
        )

    def parse(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> JobAlertParseResult:
        if not self.matches(message, content).strong:
            return JobAlertParseResult(self.provider, AlertParseStatus.UNSUPPORTED)

        soup = content_soup(content)
        candidates: list[tuple[str, Tag]] = []
        seen_hrefs: set[str] = set()
        for anchor in soup.find_all("a", href=True, limit=200):
            href = str(anchor.get("href") or "")
            if not _is_indeed_candidate(href):
                continue
            candidates.append((href, associated_card(anchor, _FIELD_SELECTORS)))
            seen_hrefs.add(href)
        for href in content.links:
            if href not in seen_hrefs and _is_indeed_candidate(href):
                candidates.append((href, soup))

        opaque_item = _opaque_table_recommendation(message, content, soup, candidates)
        if opaque_item is not None:
            return JobAlertParseResult(
                self.provider,
                AlertParseStatus.PARSED,
                items=(opaque_item,),
                examined_count=1,
            )

        items: list[JobAlertItem] = []
        seen_ids: set[str] = set()
        issues: list[str] = []
        invalid = 0
        for href, card in candidates[:50]:
            identity = canonicalize_indeed_job_url(href)
            if identity is None:
                invalid += 1
                _append_issue(issues, "malformed_or_unsafe_indeed_wrapper")
                continue
            if identity.provider_item_id in seen_ids:
                _append_issue(issues, "duplicate_provider_item")
                continue
            seen_ids.add(identity.provider_item_id)
            title = first_text(card, (".job-title", "[data-job-title]"), 200)
            if not title and len(candidates) == 1:
                title = message.subject
            company = first_text(card, (".job-company", "[data-company]"), 160)
            location = first_text(card, (".job-location", "[data-location]"), 200)
            if not title or not company or not location:
                invalid += 1
                _append_issue(issues, "missing_required_fields")
                continue
            workplace_text = first_text(
                card, (".job-workplace", "[data-workplace]"), 120
            )
            workplace, is_remote, remote_scope = workplace_evidence(
                location, workplace_text
            )
            items.append(
                JobAlertItem(
                    provider=self.provider,
                    provider_item_id=identity.provider_item_id,
                    title=title,
                    company=company,
                    location=location,
                    canonical_url=identity.canonical_url,
                    job_url=associated_direct_url(card, "indeed.com"),
                    account_id=message.account_id,
                    message_id=message.message_id,
                    posted_at=explicit_datetime(card),
                    workplace_type=workplace,
                    is_remote=is_remote,
                    remote_scope=remote_scope,
                    employment_text=first_text(
                        card, (".job-employment", "[data-employment]"), 500
                    ),
                    salary=first_text(card, (".job-salary", "[data-salary]"), 200),
                    summary=first_text(
                        card, (".job-summary", "[data-summary]"), 1_000
                    ),
                    confidence=98,
                    evidence=("indeed_sanitized_card", "stable_job_key"),
                )
            )
        return JobAlertParseResult(
            self.provider,
            AlertParseStatus.PARSED if items else AlertParseStatus.NO_ITEMS,
            items=tuple(items),
            issues=tuple(issues),
            examined_count=len(candidates[:50]),
            invalid_count=invalid,
        )


def _append_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _opaque_table_recommendation(
    message: MailMessageMetadata,
    content: BoundedMailContent,
    soup: Tag,
    candidates: list[tuple[str, Tag]],
) -> JobAlertItem | None:
    """Parse the live German single-card table when its CTS wrapper is opaque."""

    if not candidates or any(
        canonicalize_indeed_job_url(href) is not None for href, _ in candidates
    ):
        return None
    subject = _SUBJECT_RE.fullmatch(message.subject.strip())
    if subject is None:
        return None
    title = subject.group("title").strip()
    company = subject.group("company").strip()
    title_anchor = next(
        (
            anchor
            for anchor in soup.find_all("a", href=True, limit=200)
            if _is_indeed_candidate(str(anchor.get("href") or ""))
            and " ".join(anchor.get_text(" ", strip=True).split()).casefold()
            == title.casefold()
        ),
        None,
    )
    if title_anchor is None:
        return None
    company_element = next(
        (
            element
            for element in soup.find_all(("p", "span", "div"), limit=500)
            if " ".join(element.get_text(" ", strip=True).split()).casefold()
            == company.casefold()
        ),
        None,
    )
    if company_element is None:
        return None
    location = _following_location(company_element, title=title, company=company)
    href = str(title_anchor.get("href") or "")
    if not location or not _is_indeed_candidate(href):
        return None
    workplace_text = "Hybrides Arbeiten" if re.search(
        r"\bhybrides arbeiten\b", content.cleaned_text, re.I
    ) else ""
    workplace, is_remote, remote_scope = workplace_evidence(
        location, workplace_text
    )
    content_identity = alert_content_hash(title, company, location)
    employment_text = _explicit_employment_text(soup)
    return JobAlertItem(
        provider="indeed",
        provider_item_id=f"content-{content_identity}",
        title=title,
        company=company,
        location=location,
        canonical_url=_safe_indeed_search_url(title, company, location),
        account_id=message.account_id,
        message_id=message.message_id,
        posted_at=None,
        workplace_type=workplace,
        is_remote=is_remote,
        remote_scope=remote_scope,
        employment_text=employment_text,
        confidence=95,
        evidence=("indeed_table_recommendation", "stable_content_identity"),
    )


def _following_location(element: Tag, *, title: str, company: str) -> str:
    for candidate in element.find_all_next(("p", "span", "div"), limit=20):
        value = " ".join(candidate.get_text(" ", strip=True).split())[:200]
        if (
            value
            and value.casefold() not in {title.casefold(), company.casefold()}
            and _NON_LOCATION_RE.search(value) is None
        ):
            return value
    return ""


def _explicit_employment_text(soup: Tag) -> str:
    pattern = re.compile(
        r"\b(vollzeit|teilzeit|festanstellung|befristet|freelance|freiberuflich)\b",
        re.I,
    )
    for element in soup.find_all(("p", "li"), limit=500):
        value = " ".join(element.get_text(" ", strip=True).split())[:500]
        if pattern.search(value):
            return value
    return ""


def _safe_indeed_search_url(title: str, company: str, location: str) -> str:
    query = urlencode({"q": f"{title} {company}"[:300], "l": location[:200]})
    return f"https://de.indeed.com/jobs?{query}"


def _is_indeed_candidate(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return (
        (host == "cts.indeed.com" and parsed.path.startswith("/v3/"))
        or (
            (host == "indeed.com" or host.endswith(".indeed.com"))
            and parsed.path.rstrip("/") == "/viewjob"
        )
    )
