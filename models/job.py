"""Pydantic model for a job posting."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator


EmploymentRelationship: TypeAlias = Literal[
    "employee",
    "contract_employee",
    "freelance",
    "working_student",
    "internship",
    "unknown",
]
WorkSchedule: TypeAlias = Literal["full_time", "part_time", "unknown"]
ContractTerm: TypeAlias = Literal["permanent", "fixed_term", "unknown"]
PostingLanguage: TypeAlias = Literal["en", "de", "other", "unknown"]
GermanRequirementStatus: TypeAlias = Literal[
    "compatible",
    "incompatible",
    "optional",
    "unspecified",
    "unknown",
]
GermanRequirementLevel: TypeAlias = Literal[
    "none",
    "a1",
    "a2",
    "b1",
    "b2",
    "c1",
    "c2",
    "fluent",
    "business_fluent",
    "native",
    "unknown",
]
NotificationTier: TypeAlias = Literal["none", "explore", "digest", "immediate"]

EMPLOYMENT_RELATIONSHIPS = frozenset(
    {
        "employee",
        "contract_employee",
        "freelance",
        "working_student",
        "internship",
        "unknown",
    }
)
WORK_SCHEDULES = frozenset({"full_time", "part_time", "unknown"})
CONTRACT_TERMS = frozenset({"permanent", "fixed_term", "unknown"})
MAX_EMPLOYMENT_REASONS = 12
MAX_EMPLOYMENT_REASON_LENGTH = 120
MAX_EMPLOYMENT_DETAIL_LENGTH = 120
POSTING_LANGUAGES = frozenset({"en", "de", "other", "unknown"})
GERMAN_REQUIREMENT_STATUSES = frozenset(
    {"compatible", "incompatible", "optional", "unspecified", "unknown"}
)
GERMAN_REQUIREMENT_LEVELS = frozenset(
    {
        "none",
        "a1",
        "a2",
        "b1",
        "b2",
        "c1",
        "c2",
        "fluent",
        "business_fluent",
        "native",
        "unknown",
    }
)
MAX_LANGUAGE_REASONS = 8
MAX_LANGUAGE_REASON_LENGTH = 120


class Job(BaseModel):
    """Represents a single job posting, normalized across all sources."""

    id: str = ""  # SHA-256 hash of url — computed automatically
    content_hash: str = ""  # SHA-256 of title+company+location — for cross-URL dedup
    title: str
    company: str
    location: str  # raw location string from the source
    is_remote: bool = True
    workplace_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    eligible_countries: list[str] = Field(default_factory=list)
    eligible_regions: list[str] = Field(default_factory=list)
    remote_scope: Optional[str] = None  # "worldwide", "eu", "germany", "unknown"
    url: str
    description: Optional[str] = None
    salary: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source: str  # which source fetched this (e.g. "remotive")
    is_ngo: bool = False  # classified by the NGO filter
    match_score: int = 0  # 0–100% match score from filters/match.py
    match_breakdown: dict[str, int] = Field(default_factory=dict)
    match_reasons: list[str] = Field(default_factory=list)
    eligibility_status: Literal["unknown", "eligible", "ineligible"] = "unknown"
    eligibility_reasons: list[str] = Field(default_factory=list)
    notification_tier: NotificationTier = "none"
    employment_relationship: EmploymentRelationship = "unknown"
    work_schedule: WorkSchedule = "unknown"
    contract_term: ContractTerm = "unknown"
    weekly_hours: int | None = Field(default=None, ge=1, le=168)
    contract_duration: str | None = None
    freelance_rate: str | None = None
    employment_reasons: list[str] = Field(default_factory=list)
    freelance_permission_required: bool = False
    posting_language: PostingLanguage = "unknown"
    german_requirement_status: GermanRequirementStatus = "unknown"
    german_requirement_level: GermanRequirementLevel = "unknown"
    language_reasons: list[str] = Field(default_factory=list)
    company_city: Optional[str] = None
    company_postal_code: Optional[str] = None
    company_country: Optional[str] = None
    posted_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def compute_id(self) -> "Job":
        """Derive a stable id from the URL so we can deduplicate."""
        if not self.id:
            self.id = hashlib.sha256(self.url.encode()).hexdigest()
        # Secondary dedup key: normalize title+company+location
        if not self.content_hash:
            composite = f"{self.title.lower().strip()}|{self.company.lower().strip()}|{self.location.lower().strip()}"
            self.content_hash = hashlib.sha256(composite.encode()).hexdigest()
        return self

    @field_validator("title", "company", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def ensure_tags_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    @field_validator("eligible_countries", "eligible_regions", mode="before")
    @classmethod
    def ensure_normalized_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = v.split(",")
        return [str(item).strip().lower() for item in v if str(item).strip()]

    @field_validator(
        "employment_relationship", "work_schedule", "contract_term", mode="before"
    )
    @classmethod
    def default_unknown_employment_literals(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return "unknown"
        return v

    @field_validator("weekly_hours", mode="before")
    @classmethod
    def validate_weekly_hours(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("weekly_hours must be an integer from 1 to 168 or None")
        return v

    @field_validator("contract_duration", "freelance_rate", mode="before")
    @classmethod
    def bound_employment_detail(cls, v):
        if v is None:
            return None
        normalized = " ".join(str(v).split())
        return normalized[:MAX_EMPLOYMENT_DETAIL_LENGTH] or None

    @field_validator("employment_reasons", mode="before")
    @classmethod
    def normalize_employment_reasons(cls, v):
        if v is None:
            return []
        values = [v] if isinstance(v, str) else v
        reasons: list[str] = []
        for value in values:
            reason = " ".join(str(value).split())[:MAX_EMPLOYMENT_REASON_LENGTH]
            if reason and reason not in reasons:
                reasons.append(reason)
            if len(reasons) >= MAX_EMPLOYMENT_REASONS:
                break
        return reasons

    @field_validator(
        "posting_language",
        "german_requirement_status",
        "german_requirement_level",
        mode="before",
    )
    @classmethod
    def normalize_language_literals(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return "unknown"
        return str(v).strip().lower()

    @field_validator("language_reasons", mode="before")
    @classmethod
    def normalize_language_reasons(cls, v):
        if v is None:
            return []
        values = [v] if isinstance(v, str) else v
        reasons: list[str] = []
        for value in values:
            reason = " ".join(str(value).split())[:MAX_LANGUAGE_REASON_LENGTH]
            if reason and reason not in reasons:
                reasons.append(reason)
            if len(reasons) >= MAX_LANGUAGE_REASONS:
                break
        return reasons

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Job):
            return self.id == other.id
        return NotImplemented
