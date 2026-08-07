"""Typed contracts for source outcomes and scan-funnel observability."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx

from models.job import Job


MAX_COMPONENT_ISSUE_DETAILS = 5
MAX_COMPONENT_IDENTIFIER_LENGTH = 120


class SourceStatus(str, Enum):
    """Stable source-attempt outcomes persisted in scan metrics."""

    HEALTHY = "healthy"
    ZERO_RESULTS = "zero_results"
    PARTIAL_SUCCESS = "partial_success"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    UNKNOWN_ERROR = "unknown_error"


class SourceComponentError(RuntimeError):
    """A component failure whose source status is known without an HTTP code."""

    def __init__(self, status: SourceStatus, explanation: str) -> None:
        super().__init__(explanation)
        self.status = status


class RejectionCode(str, Enum):
    """The single terminal reason assigned to a rejected raw job."""

    DUPLICATE_IN_MEMORY = "duplicate_in_memory"
    COMPANY_BLOCKLIST = "company_blocklist"
    LOCATION = "location"
    EMPLOYMENT_RELATIONSHIP = "employment_relationship"
    ROLE = "role"
    STACK = "stack"
    LANGUAGE = "language"
    SENIORITY = "seniority"
    SALARY = "salary"
    RECENCY = "recency"
    MINIMUM_SCORE = "minimum_score"
    COMPANY_CAP = "company_cap"


USABLE_SOURCE_STATUSES = frozenset(
    {
        SourceStatus.HEALTHY,
        SourceStatus.ZERO_RESULTS,
        SourceStatus.PARTIAL_SUCCESS,
    }
)
FULLY_SUCCESSFUL_SOURCE_STATUSES = frozenset(
    {SourceStatus.HEALTHY, SourceStatus.ZERO_RESULTS}
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|client[_-]?secret|oauth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"webhook(?:[_-]?url)?)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+")


def _sanitize_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if "hooks.slack.com" in host or (
            ("discord.com" in host or "discordapp.com" in host)
            and "/webhook" in parsed.path.lower()
        ):
            return "[REDACTED_WEBHOOK_URL]" + trailing
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) + trailing
    except ValueError:
        return "[REDACTED_URL]" + trailing


def sanitize_source_error(value: BaseException | str | None, limit: int = 300) -> str | None:
    """Return bounded diagnostic text safe for persistence and display."""

    if value is None:
        return None
    text = str(value)
    text = _URL_RE.sub(_sanitize_url, text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return None
    return text[: max(0, limit)]


def sanitize_component_identifier(value: object) -> str:
    """Return a short non-sensitive board, page, endpoint, or query label."""

    return (
        sanitize_source_error(str(value), limit=MAX_COMPONENT_IDENTIFIER_LENGTH)
        or "unknown_component"
    )


def classify_source_exception(exc: BaseException) -> SourceStatus:
    """Classify a complete-source exception without exposing its contents."""

    if isinstance(exc, SourceComponentError):
        return exc.status
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return SourceStatus.RATE_LIMITED
        if status in {401, 403, 407, 451}:
            return SourceStatus.BLOCKED
        if status >= 500:
            return SourceStatus.NETWORK_ERROR
        return SourceStatus.UNKNOWN_ERROR
    if isinstance(exc, httpx.DecodingError):
        return SourceStatus.PARSE_ERROR
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, httpx.RequestError)):
        return SourceStatus.NETWORK_ERROR
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError, UnicodeError)):
        return SourceStatus.PARSE_ERROR
    return SourceStatus.UNKNOWN_ERROR


_COMPLETE_FAILURE_PRECEDENCE = (
    SourceStatus.RATE_LIMITED,
    SourceStatus.BLOCKED,
    SourceStatus.NETWORK_ERROR,
    SourceStatus.PARSE_ERROR,
    SourceStatus.UNKNOWN_ERROR,
)


def dominant_failure_status(
    issues: Iterable["SanitizedSourceIssue"],
) -> SourceStatus:
    """Choose a deterministic status when every attempted component failed.

    Actionable external causes take precedence: rate limiting, blocking,
    networking, parsing, then an unknown failure.
    """

    statuses = {issue.status for issue in issues}
    for status in _COMPLETE_FAILURE_PRECEDENCE:
        if status in statuses:
            return status
    return SourceStatus.UNKNOWN_ERROR


@dataclass(frozen=True, slots=True)
class SanitizedSourceIssue:
    """A bounded, already-sanitized source issue."""

    status: SourceStatus
    explanation: str
    component: str = ""

    @classmethod
    def from_error(
        cls,
        error: BaseException | str,
        status: SourceStatus | None = None,
        component: object | None = None,
    ) -> "SanitizedSourceIssue":
        resolved = status or (
            classify_source_exception(error)
            if isinstance(error, BaseException)
            else SourceStatus.UNKNOWN_ERROR
        )
        return cls(
            resolved,
            sanitize_source_error(error) or resolved.value,
            sanitize_component_identifier(component) if component is not None else "",
        )

    @property
    def summary(self) -> str:
        if not self.component:
            return self.explanation
        return f"{self.component} [{self.status.value}]: {self.explanation}"


@dataclass(slots=True)
class SourceFetchOutcome:
    """Result of one complete source attempt."""

    source: str
    jobs: list[Job]
    status: SourceStatus
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    issues: tuple[SanitizedSourceIssue, ...] = ()
    component_issue_count: int = 0

    @property
    def raw_count(self) -> int:
        return len(self.jobs)

    @property
    def issue_count(self) -> int:
        return max(self.component_issue_count, len(self.issues))

    @property
    def sanitized_error(self) -> str | None:
        if not self.issues:
            return None
        return sanitize_source_error("; ".join(issue.summary for issue in self.issues))


@dataclass(frozen=True, slots=True)
class FilterRejection:
    """Stable rejection code plus a human-readable explanation."""

    code: RejectionCode
    explanation: str


@dataclass(frozen=True, slots=True)
class TerminalFilterResult:
    """The terminal accepted/rejected decision for one raw job."""

    source: str
    accepted: bool
    rejection: FilterRejection | None = None


SourceIssue = SanitizedSourceIssue
FilterDecision = TerminalFilterResult
FilterResult = TerminalFilterResult


def empty_rejection_counts() -> dict[RejectionCode, int]:
    return {code: 0 for code in RejectionCode}


def empty_routing_counts() -> dict[str, int]:
    return {"immediate": 0, "digest": 0, "explore": 0, "diagnostic": 0}


@dataclass(slots=True)
class SourceFunnelMetrics:
    """Fetch outcome and funnel counts attributable to one source."""

    source: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    status: SourceStatus
    raw_count: int = 0
    accepted_count: int = 0
    unseen_count: int = 0
    saved_count: int = 0
    rejection_counts: dict[RejectionCode, int] = field(default_factory=empty_rejection_counts)
    routing_counts: dict[str, int] = field(default_factory=empty_routing_counts)
    issue_count: int = 0
    sanitized_error: str | None = None

    @property
    def rejected_count(self) -> int:
        return sum(self.rejection_counts.values())

    def validate_accounting(self) -> None:
        if self.raw_count != self.accepted_count + self.rejected_count:
            raise ValueError(
                f"source accounting mismatch for {self.source}: "
                f"{self.raw_count} != {self.accepted_count} + {self.rejected_count}"
            )

    def rejection_counts_json(self) -> str:
        return json.dumps(
            {code.value: count for code, count in self.rejection_counts.items() if count},
            sort_keys=True,
        )

    def routing_counts_json(self) -> str:
        return json.dumps(
            {name: count for name, count in self.routing_counts.items() if count},
            sort_keys=True,
        )


# The plan and objective use both names for this same per-source contract.
PerSourceFunnelMetrics = SourceFunnelMetrics
PerSourceScanSummary = SourceFunnelMetrics


@dataclass(slots=True)
class FilterRunSummary:
    """Result of the one global filter pass."""

    accepted_jobs: list[Job]
    raw_count: int
    rejection_counts: dict[RejectionCode, int]
    per_source: dict[str, SourceFunnelMetrics]
    verbose_rejections: list[tuple[Job, FilterRejection]] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_jobs)

    @property
    def rejected_count(self) -> int:
        return sum(self.rejection_counts.values())

    def validate_accounting(self) -> None:
        if self.raw_count != self.accepted_count + self.rejected_count:
            raise ValueError(
                f"overall accounting mismatch: {self.raw_count} != "
                f"{self.accepted_count} + {self.rejected_count}"
            )
        for metrics in self.per_source.values():
            metrics.validate_accounting()


@dataclass(slots=True)
class ScanSummary:
    """Overall production scan summary backed by one row per source."""

    scan_id: str
    started_at: datetime
    completed_at: datetime
    sources: dict[str, SourceFunnelMetrics]

    @property
    def raw_count(self) -> int:
        return sum(item.raw_count for item in self.sources.values())

    @property
    def accepted_count(self) -> int:
        return sum(item.accepted_count for item in self.sources.values())

    @property
    def unseen_count(self) -> int:
        return sum(item.unseen_count for item in self.sources.values())

    @property
    def saved_count(self) -> int:
        return sum(item.saved_count for item in self.sources.values())

    @property
    def rejection_counts(self) -> dict[RejectionCode, int]:
        counts: Counter[RejectionCode] = Counter()
        for item in self.sources.values():
            counts.update(item.rejection_counts)
        return {code: counts.get(code, 0) for code in RejectionCode}

    @property
    def routing_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for item in self.sources.values():
            counts.update(item.routing_counts)
        return {
            name: counts.get(name, 0)
            for name in ("immediate", "digest", "explore", "diagnostic")
        }

    @property
    def rejected_count(self) -> int:
        return sum(self.rejection_counts.values())

    def validate_accounting(self) -> None:
        for metrics in self.sources.values():
            metrics.validate_accounting()
        if self.raw_count != self.accepted_count + self.rejected_count:
            raise ValueError("overall scan accounting mismatch")

    def to_health_dict(self, source_health: Iterable[dict] | None = None) -> dict:
        """Build the additive, backward-compatible health summary."""

        source_health_map: dict[str, dict] = {}
        if source_health is None:
            for name, metrics in self.sources.items():
                source_health_map[name] = {
                    "status": metrics.status.value,
                    "raw": metrics.raw_count,
                    "accepted": metrics.accepted_count,
                    "saved": metrics.saved_count,
                    "issue_count": metrics.issue_count,
                    "sanitized_error": sanitize_source_error(metrics.sanitized_error),
                    "last_completed_at": metrics.completed_at.isoformat(),
                    "last_usable_at": (
                        metrics.completed_at.isoformat()
                        if metrics.status in USABLE_SOURCE_STATUSES
                        else None
                    ),
                    "last_fully_successful_at": (
                        metrics.completed_at.isoformat()
                        if metrics.status in FULLY_SUCCESSFUL_SOURCE_STATUSES
                        else None
                    ),
                }
        else:
            for item in source_health:
                name = str(item.get("source", "unknown"))
                source_health_map[name] = {
                    key: item.get(key)
                    for key in (
                        "status",
                        "raw",
                        "accepted",
                        "saved",
                        "issue_count",
                        "sanitized_error",
                        "last_completed_at",
                        "last_usable_at",
                        "last_fully_successful_at",
                    )
                }

        routing = self.routing_counts
        return {
            "raw": self.raw_count,
            "eligible_role_matches": self.accepted_count,
            "rejected": self.rejected_count,
            "immediate": routing["immediate"],
            "digest": routing["digest"],
            "explore": routing["explore"],
            "diagnostic": routing["diagnostic"],
            "sources": {name: item.raw_count for name, item in self.sources.items()},
            "accepted": self.accepted_count,
            "unseen": self.unseen_count,
            "saved": self.saved_count,
            "rejection_counts": {
                code.value: count for code, count in self.rejection_counts.items() if count
            },
            "source_health": source_health_map,
        }


OverallScanSummary = ScanSummary


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
