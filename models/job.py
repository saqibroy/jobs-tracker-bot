"""Pydantic model for a job posting."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Job(BaseModel):
    """Represents a single job posting, normalized across all sources."""

    id: str = ""  # SHA-256 hash of url — computed automatically
    content_hash: str = ""  # SHA-256 of title+company+location — for cross-URL dedup
    title: str
    company: str
    location: str  # raw location string from the source
    is_remote: bool = True
    remote_scope: Optional[str] = None  # "worldwide", "eu", "germany", "unknown"
    url: str
    description: Optional[str] = None
    salary: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    source: str  # which source fetched this (e.g. "remotive")
    is_ngo: bool = False  # classified by the NGO filter
    match_score: int = 0  # 0–100% match score from filters/match.py
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

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Job):
            return self.id == other.id
        return NotImplemented
