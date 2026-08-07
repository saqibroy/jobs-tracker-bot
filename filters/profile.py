"""Load the sanitized candidate profile used by role and match evaluation."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments
    import tomli as tomllib
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from models.job import (
    EMPLOYMENT_RELATIONSHIPS,
    WORK_SCHEDULES,
    EmploymentRelationship,
    WorkSchedule,
)
from filters.notification_policy import NotificationPolicy

GermanCefrLevel = Literal["a1", "a2", "b1", "b2", "c1", "c2"]
GERMAN_CEFR_LEVELS = frozenset({"a1", "a2", "b1", "b2", "c1", "c2"})


DEFAULT_EMPLOYMENT_SECTION: dict[str, Any] = {
    "accepted_relationships": [
        "employee", "contract_employee", "freelance", "unknown",
    ],
    "rejected_relationships": ["working_student", "internship"],
    "accepted_schedules": ["full_time", "part_time", "unknown"],
    "freelance_permission_required": True,
    "preferred_weekly_hours_min": 15,
    "preferred_weekly_hours_max": 40,
}

DEFAULT_NOTIFICATION_SECTION: dict[str, Any] = {
    "immediate_score": 70,
    "digest_score": 45,
    "explore_score": 30,
    "daily_explore_enabled": True,
    "explore_hour_utc": 17,
    "immediate_max_items": 15,
    "digest_max_items": 15,
    "explore_max_items": 10,
    "pending_max_age_days": 14,
    "max_jobs_per_company": 2,
    "freelance_permission_max_tier": "digest",
}


@dataclass(frozen=True)
class EmploymentPolicy:
    """Validated profile-driven compatibility policy for employment metadata."""

    accepted_relationships: frozenset[EmploymentRelationship]
    rejected_relationships: frozenset[EmploymentRelationship]
    accepted_schedules: frozenset[WorkSchedule]
    freelance_permission_required: bool
    preferred_weekly_hours_min: int | None
    preferred_weekly_hours_max: int | None


@dataclass(frozen=True)
class LanguagePolicy:
    """Validated candidate language capability used by the language gate."""

    max_german_level: GermanCefrLevel
    accepted_languages: frozenset[str]


def parse_notification_policy(
    section: Mapping[str, Any] | None,
) -> NotificationPolicy:
    """Validate the centralized notification policy from the candidate profile."""

    values = DEFAULT_NOTIFICATION_SECTION if section is None else section
    try:
        return NotificationPolicy(
            immediate_score=values.get("immediate_score"),
            digest_score=values.get("digest_score"),
            explore_score=values.get("explore_score"),
            daily_explore_enabled=values.get("daily_explore_enabled"),
            explore_hour_utc=values.get("explore_hour_utc"),
            immediate_max_items=values.get("immediate_max_items"),
            digest_max_items=values.get("digest_max_items"),
            explore_max_items=values.get("explore_max_items"),
            pending_max_age_days=values.get("pending_max_age_days"),
            max_jobs_per_company=values.get("max_jobs_per_company"),
            freelance_permission_max_tier=values.get(
                "freelance_permission_max_tier"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid notification policy: {exc}") from exc


def parse_language_policy(section: Mapping[str, Any] | None) -> LanguagePolicy:
    """Validate the required candidate language settings."""

    if section is None:
        raise ValueError("candidate.max_german_level is required")
    raw_maximum = section.get("max_german_level")
    if raw_maximum is None or not str(raw_maximum).strip():
        raise ValueError("candidate.max_german_level is required")
    maximum = str(raw_maximum).strip().lower()
    if maximum not in GERMAN_CEFR_LEVELS:
        raise ValueError(
            "candidate.max_german_level must be one of A1, A2, B1, B2, C1, C2"
        )
    raw_languages = section.get("accepted_languages", [])
    if not isinstance(raw_languages, list):
        raise ValueError("candidate.accepted_languages must be a list")
    accepted = frozenset(
        str(value).strip().lower() for value in raw_languages if str(value).strip()
    )
    return LanguagePolicy(
        max_german_level=cast(GermanCefrLevel, maximum),
        accepted_languages=accepted,
    )


def _literal_set(
    section: Mapping[str, Any],
    key: str,
    supported: frozenset[str],
) -> frozenset[str]:
    raw = section.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"employment.{key} must be a list")
    values = frozenset(str(value).strip().lower() for value in raw)
    invalid = sorted(values - supported)
    if invalid:
        raise ValueError(
            f"employment.{key} contains unsupported value(s): {', '.join(invalid)}"
        )
    return values


def _optional_hours(section: Mapping[str, Any], key: str) -> int | None:
    value = section.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 168:
        raise ValueError(f"employment.{key} must be an integer from 1 to 168")
    return value


def parse_employment_policy(section: Mapping[str, Any] | None) -> EmploymentPolicy:
    """Validate an employment profile section and return its typed policy."""

    values: Mapping[str, Any] = (
        DEFAULT_EMPLOYMENT_SECTION if section is None else section
    )
    accepted = _literal_set(values, "accepted_relationships", EMPLOYMENT_RELATIONSHIPS)
    rejected = _literal_set(values, "rejected_relationships", EMPLOYMENT_RELATIONSHIPS)
    schedules = _literal_set(values, "accepted_schedules", WORK_SCHEDULES)
    overlap = sorted(accepted & rejected)
    if overlap:
        raise ValueError(
            "employment accepted_relationships and rejected_relationships overlap: "
            + ", ".join(overlap)
        )
    permission_required = values.get("freelance_permission_required", False)
    if not isinstance(permission_required, bool):
        raise ValueError("employment.freelance_permission_required must be a boolean")
    minimum = _optional_hours(values, "preferred_weekly_hours_min")
    maximum = _optional_hours(values, "preferred_weekly_hours_max")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            "employment.preferred_weekly_hours_min must not exceed "
            "preferred_weekly_hours_max"
        )
    return EmploymentPolicy(
        accepted_relationships=cast(frozenset[EmploymentRelationship], accepted),
        rejected_relationships=cast(frozenset[EmploymentRelationship], rejected),
        accepted_schedules=cast(frozenset[WorkSchedule], schedules),
        freelance_permission_required=permission_required,
        preferred_weekly_hours_min=minimum,
        preferred_weekly_hours_max=maximum,
    )


@lru_cache(maxsize=1)
def load_profile() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "profile.toml"
    with path.open("rb") as handle:
        profile = tomllib.load(handle)
    parse_employment_policy(profile.get("employment"))
    parse_language_policy(profile.get("candidate"))
    parse_notification_policy(profile.get("notifications"))
    return profile


def profile_list(section: str, key: str) -> list[str]:
    values = load_profile().get(section, {}).get(key, [])
    return [str(value).lower() for value in values]


def profile_value(section: str, key: str, default: Any = None) -> Any:
    return load_profile().get(section, {}).get(key, default)


def load_employment_policy() -> EmploymentPolicy:
    """Return the current validated employment policy."""

    return parse_employment_policy(load_profile().get("employment"))


@lru_cache(maxsize=1)
def load_language_policy() -> LanguagePolicy:
    """Return the current validated candidate language policy."""

    return parse_language_policy(load_profile().get("candidate"))


@lru_cache(maxsize=1)
def load_notification_policy() -> NotificationPolicy:
    """Return the current validated notification policy."""

    return parse_notification_policy(load_profile().get("notifications"))
