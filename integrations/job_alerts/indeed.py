"""Offline parser for sanitized, user-owned Indeed recommendation structures."""

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
from integrations.job_alerts.urls import canonicalize_indeed_job_url

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
