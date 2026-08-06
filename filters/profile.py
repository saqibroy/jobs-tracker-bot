"""Load the sanitized candidate profile used by role and match evaluation."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments
    import tomli as tomllib
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from models.job import (
    EMPLOYMENT_RELATIONSHIPS,
    WORK_SCHEDULES,
    EmploymentRelationship,
    WorkSchedule,
)


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


@dataclass(frozen=True)
class EmploymentPolicy:
    """Validated profile-driven compatibility policy for employment metadata."""

    accepted_relationships: frozenset[EmploymentRelationship]
    rejected_relationships: frozenset[EmploymentRelationship]
    accepted_schedules: frozenset[WorkSchedule]
    freelance_permission_required: bool
    preferred_weekly_hours_min: int | None
    preferred_weekly_hours_max: int | None


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
    return profile


def profile_list(section: str, key: str) -> list[str]:
    values = load_profile().get(section, {}).get(key, [])
    return [str(value).lower() for value in values]


def profile_value(section: str, key: str, default: Any = None) -> Any:
    return load_profile().get(section, {}).get(key, default)


def load_employment_policy() -> EmploymentPolicy:
    """Return the current validated employment policy."""

    return parse_employment_policy(load_profile().get("employment"))
