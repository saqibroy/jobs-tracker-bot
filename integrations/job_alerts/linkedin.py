"""Offline parser for sanitized, user-owned LinkedIn alert structures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

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
from integrations.job_alerts.urls import canonicalize_linkedin_job_url

_SENDER_RE = re.compile(r"(?:^|<)jobalerts-noreply@linkedin\.com(?:>|$)", re.I)
_ALERT_HEADING_RE = re.compile(r"\byour job alert for\b", re.I)
_FOOTER_RE = re.compile(r"\byou are receiving job alert emails\.?", re.I)
_NEGATIVE_RE = re.compile(
    r"\bland a job faster\b|\bhiring.manager recommendation\b", re.I
)
_LIFECYCLE_RE = re.compile(
    r"\b(your application|application (?:received|update|status)|"
    r"applied to|your interview|recruiter (?:message|outreach)|inmail|"
    r"message from a recruiter)\b",
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


@dataclass(frozen=True, slots=True)
class LinkedInAlertParser:
    provider: str = "linkedin"

    def matches(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch:
        text = content.cleaned_text
        sender = bool(_SENDER_RE.search(message.sender.strip()))
        heading = bool(_ALERT_HEADING_RE.search(text))
        footer = bool(_FOOTER_RE.search(text))
        has_job_link = any(
            canonicalize_linkedin_job_url(link) is not None for link in content.links
        )
        excluded = bool(_NEGATIVE_RE.search(text)) or bool(
            _LIFECYCLE_RE.search(" ".join((message.subject, text)))
        )
        matched = sender and heading and footer and has_job_link and not excluded
        evidence = tuple(
            code
            for code, present in (
                ("linkedin_alert_sender", sender),
                ("linkedin_alert_heading", heading),
                ("linkedin_alert_footer", footer),
                ("linkedin_numeric_job_link", has_job_link),
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
            if not _is_linkedin_candidate(href):
                continue
            candidates.append((href, associated_card(anchor, _FIELD_SELECTORS)))
            seen_hrefs.add(href)
        for href in content.links:
            if href not in seen_hrefs and _is_linkedin_candidate(href):
                candidates.append((href, soup))

        items: list[JobAlertItem] = []
        seen_ids: set[str] = set()
        issues: list[str] = []
        invalid = 0
        for href, card in candidates[:50]:
            identity = canonicalize_linkedin_job_url(href)
            if identity is None:
                invalid += 1
                _append_issue(issues, "unsafe_or_unrecognized_job_url")
                continue
            job_id, canonical_url = identity
            if job_id in seen_ids:
                _append_issue(issues, "duplicate_provider_item")
                continue
            seen_ids.add(job_id)
            title = first_text(card, (".job-title", "[data-job-title]"), 200)
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
                    provider_item_id=job_id,
                    title=title,
                    company=company,
                    location=location,
                    canonical_url=canonical_url,
                    job_url=associated_direct_url(card, "linkedin.com"),
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
                    evidence=("linkedin_sanitized_card", "numeric_provider_id"),
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


def _is_linkedin_candidate(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return (
        (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and re.search(r"/(?:comm/)?jobs/view/", parsed.path) is not None
    )
