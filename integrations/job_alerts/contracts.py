"""Typed and bounded provider-neutral mail-alert contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol

from integrations.job_alerts.urls import normalize_alert_url

MAX_CONTENT_BYTES = 512 * 1024
MAX_LINKS = 200
MAX_ALERT_ITEMS_PER_MESSAGE = 50
MAX_ALERT_ITEMS_PER_SYNC = 500
MAX_TITLE = 200
MAX_COMPANY = 160
MAX_LOCATION = 200
MAX_URL = 1_000
MAX_SUMMARY = 1_000
MAX_SALARY = 200
MAX_EVIDENCE = 8
MAX_EVIDENCE_LENGTH = 120
MAX_ISSUES = 10
MAX_ISSUE_LENGTH = 160
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")


def bounded_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def bounded_values(
    values: tuple[str, ...] | list[str],
    *,
    count: int,
    length: int,
) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = bounded_text(value, length)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= count:
            break
    return tuple(result)


def _provider_name(value: object, *, required: bool) -> str:
    provider = bounded_text(value, 80).lower()
    if provider and _PROVIDER_RE.fullmatch(provider) is None:
        raise ValueError("provider must be a stable lowercase identifier")
    if required and not provider:
        raise ValueError("provider is required")
    return provider


class MailIntent(str, Enum):
    APPLICATION_OR_RECRUITMENT = "application_or_recruitment"
    JOB_ALERT = "job_alert"
    UNKNOWN_JOB_EMAIL = "unknown_job_email"


@dataclass(frozen=True, slots=True)
class MailMessageMetadata:
    account_id: str
    message_id: str
    folder_id: str = ""
    folder_name: str = ""
    subject: str = ""
    sender: str = ""
    summary: str = ""
    message_date: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", bounded_text(self.account_id, 200))
        object.__setattr__(self, "message_id", bounded_text(self.message_id, 200))
        object.__setattr__(self, "folder_id", bounded_text(self.folder_id, 200))
        object.__setattr__(self, "folder_name", bounded_text(self.folder_name, 200))
        object.__setattr__(self, "subject", bounded_text(self.subject, 500))
        object.__setattr__(self, "sender", bounded_text(self.sender, 320))
        object.__setattr__(self, "summary", bounded_text(self.summary, MAX_SUMMARY))


@dataclass(frozen=True, slots=True)
class BoundedMailContent:
    """One cleaned representation derived from one bounded full-body fetch."""

    sanitized_html: str
    cleaned_text: str
    links: tuple[str, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "links", tuple(self.links[:MAX_LINKS]))


@dataclass(frozen=True, slots=True)
class AlertMatch:
    provider: str
    matched: bool
    confidence: int = 0
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider_name(self.provider, required=True))
        object.__setattr__(self, "confidence", max(0, min(100, int(self.confidence))))
        object.__setattr__(
            self,
            "evidence",
            bounded_values(
                self.evidence,
                count=MAX_EVIDENCE,
                length=MAX_EVIDENCE_LENGTH,
            ),
        )

    @property
    def strong(self) -> bool:
        return self.matched and self.confidence >= 80


@dataclass(frozen=True, slots=True)
class MailIntentDecision:
    intent: MailIntent
    provider: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider_name(self.provider, required=False))
        object.__setattr__(
            self,
            "evidence",
            bounded_values(
                self.evidence,
                count=MAX_EVIDENCE,
                length=MAX_EVIDENCE_LENGTH,
            ),
        )


@dataclass(frozen=True, slots=True)
class JobAlertItem:
    provider: str
    title: str
    company: str
    location: str
    canonical_url: str
    account_id: str
    message_id: str
    provider_item_id: str = ""
    job_url: str = ""
    posted_at: datetime | None = None
    workplace_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    is_remote: bool | None = None
    remote_scope: str | None = None
    eligible_countries: tuple[str, ...] = ()
    eligible_regions: tuple[str, ...] = ()
    employment_text: str = ""
    salary: str = ""
    summary: str = ""
    confidence: int = 0
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider = _provider_name(self.provider, required=True)
        title = bounded_text(self.title, MAX_TITLE)
        company = bounded_text(self.company, MAX_COMPANY)
        location = bounded_text(self.location, MAX_LOCATION)
        url = normalize_alert_url(self.canonical_url)
        if not provider or not title or not company or not location or not url:
            raise ValueError("alert item requires provider, title, company, location, and safe URL")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "company", company)
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "canonical_url", url)
        object.__setattr__(self, "account_id", bounded_text(self.account_id, 200))
        object.__setattr__(self, "message_id", bounded_text(self.message_id, 200))
        object.__setattr__(self, "provider_item_id", bounded_text(self.provider_item_id, 200))
        direct_url = normalize_alert_url(self.job_url) if self.job_url else None
        if self.job_url and direct_url is None:
            raise ValueError("alert item direct job URL must be safe HTTP(S)")
        object.__setattr__(self, "job_url", direct_url or "")
        object.__setattr__(self, "remote_scope", bounded_text(self.remote_scope, 40) or None)
        object.__setattr__(
            self,
            "eligible_countries",
            tuple(bounded_text(value, 80).lower() for value in self.eligible_countries[:20]),
        )
        object.__setattr__(
            self,
            "eligible_regions",
            tuple(bounded_text(value, 80).lower() for value in self.eligible_regions[:20]),
        )
        object.__setattr__(self, "employment_text", bounded_text(self.employment_text, 500))
        object.__setattr__(self, "salary", bounded_text(self.salary, MAX_SALARY))
        object.__setattr__(self, "summary", bounded_text(self.summary, MAX_SUMMARY))
        object.__setattr__(self, "confidence", max(0, min(100, int(self.confidence))))
        object.__setattr__(
            self,
            "evidence",
            bounded_values(self.evidence, count=MAX_EVIDENCE, length=MAX_EVIDENCE_LENGTH),
        )


class AlertParseStatus(str, Enum):
    PARSED = "parsed"
    NO_ITEMS = "no_items"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class JobAlertParseResult:
    provider: str
    status: AlertParseStatus
    items: tuple[JobAlertItem, ...] = ()
    issues: tuple[str, ...] = ()
    examined_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0

    def __post_init__(self) -> None:
        items = tuple(self.items[:MAX_ALERT_ITEMS_PER_MESSAGE])
        object.__setattr__(self, "provider", _provider_name(self.provider, required=True))
        object.__setattr__(self, "items", items)
        object.__setattr__(
            self,
            "issues",
            bounded_values(self.issues, count=MAX_ISSUES, length=MAX_ISSUE_LENGTH),
        )
        valid = len(items)
        invalid = min(
            max(0, MAX_ALERT_ITEMS_PER_MESSAGE - valid),
            max(0, int(self.invalid_count)),
        )
        examined = min(
            MAX_ALERT_ITEMS_PER_MESSAGE,
            max(valid + invalid, max(0, int(self.examined_count))),
        )
        object.__setattr__(self, "examined_count", examined)
        object.__setattr__(self, "valid_count", valid)
        object.__setattr__(self, "invalid_count", invalid)


class AlertParser(Protocol):
    provider: str

    def matches(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch: ...

    def parse(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> JobAlertParseResult: ...
