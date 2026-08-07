"""Shared employment classification, compatibility policy, and presentation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from filters.profile import EmploymentPolicy, load_employment_policy
from models.job import (
    CONTRACT_TERMS,
    EMPLOYMENT_RELATIONSHIPS,
    MAX_EMPLOYMENT_DETAIL_LENGTH,
    MAX_EMPLOYMENT_REASON_LENGTH,
    MAX_EMPLOYMENT_REASONS,
    WORK_SCHEDULES,
    ContractTerm,
    EmploymentRelationship,
    Job,
    WorkSchedule,
)


@dataclass(frozen=True)
class EmploymentStructuredInput:
    """Optional normalized values supplied authoritatively by a source.

    Phase 2A adapters do not populate this object. Phase 2B can supply any
    supported subset; invalid values are ignored independently so another
    valid structured dimension and heuristic fallbacks remain usable.
    """

    employment_relationship: EmploymentRelationship | None = None
    work_schedule: WorkSchedule | None = None
    contract_term: ContractTerm | None = None
    weekly_hours: int | None = None
    contract_duration: str | None = None
    freelance_rate: str | None = None


StructuredEmployment = EmploymentStructuredInput | Mapping[str, object]

_STRUCTURED_DIMENSIONS = (
    "employment_relationship",
    "work_schedule",
    "contract_term",
    "weekly_hours",
    "contract_duration",
    "freelance_rate",
)

_WORKING_STUDENT_RE = re.compile(
    r"\bworking[\s-]+student\b|\bwerkstudent(?:[/*:_-]?in)?(?:nen)?\b",
    re.IGNORECASE,
)
_INTERNSHIP_RE = re.compile(
    r"\bintern(?:ship|ships)?\b|\bpraktikum\b|"
    r"\bpraktikant(?:[/*:_-]?in)?(?:nen)?\b",
    re.IGNORECASE,
)
_FREELANCE_RE = re.compile(
    r"\bfreelanc(?:e|er|ers|ing)\b|\bfreiberuflich(?:e[rsn]?)?\b|"
    r"\bfreiberufler(?:[/*:_-]?in)?(?:nen)?\b|"
    r"\bselbstst(?:ä|ae)ndig(?:e[rsn]?)?\b|\bb2b\b|"
    r"\bself[\s-]+employed\b|\bindependent[\s-]+contractor\b|"
    r"\bcontractor\s+(?:basis|only)\b",
    re.IGNORECASE,
)
_CONTRACT_EMPLOYEE_RE = re.compile(
    r"\barbeitnehmer(?:überlassung|ueberlassung)\b|"
    r"\btemporary[\s-]+agency[\s-]+employment\b|"
    r"\bagency[\s-]+employee\b|"
    r"\bemploy(?:ed|ment)\s+through\s+(?:a\s+)?staffing\s+agency\b|"
    r"\bstaffing[\s-]+agency\s+employee\b|\bcontract[\s-]+employee\b",
    re.IGNORECASE,
)
_EMPLOYEE_RE = re.compile(
    r"\bdirect[\s-]+employment\b|\bemployment\s+contract\b|"
    r"\bemploy(?:ed|ment)\s+(?:directly\s+)?(?:by|with)\b|"
    r"\bas\s+an?\s+employee\b|\bemployee\s+(?:position|role)\b|"
    r"\bfestanstellung\b|\banstellung\b|\bangestelltenverh(?:ä|ae)ltnis\b|"
    r"\bsozialversicherungspflichtige\s+besch(?:ä|ae)ftigung\b",
    re.IGNORECASE,
)

_FULL_TIME_RE = re.compile(r"\bfull[\s-]?time\b|\bvollzeit\b", re.IGNORECASE)
_PART_TIME_RE = re.compile(r"\bpart[\s-]?time\b|\bteilzeit\b", re.IGNORECASE)
_PERMANENT_RE = re.compile(
    r"\bpermanent(?:ly)?\b|\bunbefristet(?:e[rsn]?)?\b|"
    r"\bopen[\s-]+ended\s+(?:contract|employment)\b",
    re.IGNORECASE,
)
_FIXED_TERM_RE = re.compile(
    r"\bfixed[\s-]+term\b|\bbefristet(?:e[rsn]?)?\b|"
    r"\blimited[\s-]+term\s+(?:contract|employment|role)\b",
    re.IGNORECASE,
)

_HOURS_RANGE_RE = re.compile(
    r"(?<![\w])(?P<low>\d{1,3})\s*(?:-|–|—|to|bis)\s*"
    r"(?P<high>\d{1,3})\s*(?:h(?:ours?)?|hrs?\.?|stunden|std\.?)\s*"
    r"(?:/|per\s+|pro\s+)?(?:week|woche|wk)\b",
    re.IGNORECASE,
)
_HOURS_SINGLE_RE = re.compile(
    r"(?<![\w])(?P<hours>\d{1,3})\s*(?:h|hrs?\.?|hours?|stunden|std\.?)\s*"
    r"(?:/|per\s+|pro\s+)(?:week|woche|wk)\b",
    re.IGNORECASE,
)
_HOURS_COMPACT_RE = re.compile(r"(?<![\w])(?P<hours>\d{1,3})\s*h(?![\w/])", re.IGNORECASE)
_COMPACT_HOURS_CONTEXT_RE = re.compile(
    r"working\s+hours|weekly|week|woche|wochenstunden|schedule|"
    r"part[\s-]?time|full[\s-]?time|teilzeit|vollzeit",
    re.IGNORECASE,
)
_COMPACT_HOURS_ID_RE = re.compile(r"(?:\bid|reference|ref|code|ticket)\s*[:#-]?\s*$", re.IGNORECASE)
_TITLE_ROLE_CONTEXT_RE = re.compile(
    r"\b(?:developer|engineer|role|position|stelle|designer|manager|consultant)\b",
    re.IGNORECASE,
)

_DURATION_PATTERNS = (
    re.compile(
        r"(?<![\w])(?P<count>\d{1,3})\s*[- ]\s*"
        r"(?P<unit>months?|weeks?|years?)\s+"
        r"(?:fixed[\s-]+term\s+)?(?:contract|role|position|employment)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:contract|role|position|employment)\s+(?:lasting\s+|for\s+)"
        r"(?P<count>\d{1,3})\s+(?P<unit>months?|weeks?|years?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:auf|f(?:ü|ue)r)\s+(?P<count>\d{1,3})\s+"
        r"(?P<unit>monate?|wochen?|jahre?)\s+befristet\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w])(?P<count>\d{1,3})\s*[- ]\s*"
        r"(?P<unit>monat)(?:ige[rsn]?|igen)?\s+"
        r"(?:vertrag|stelle|position)\b",
        re.IGNORECASE,
    ),
)

_RATE_LABEL_RE = re.compile(
    r"\b(?P<label>day\s+rate|daily\s+rate|hourly\s+freelance\s+rate|"
    r"freelance\s+hourly\s+rate|tagessatz|stundensatz)\b\s*[:=-]?\s*"
    r"(?P<currency_before>€|eur|usd|\$|gbp|£)?\s*"
    r"(?P<amount>\d{1,5}(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency_after>€|eur|usd|\$|gbp|£)?",
    re.IGNORECASE,
)
_RATE_SLASH_RE = re.compile(
    r"(?P<currency_before>€|eur|usd|\$|gbp|£)\s*"
    r"(?P<amount>\d{1,5}(?:[.,]\d{1,2})?)\s*"
    r"(?:/|per\s+|pro\s+)(?P<period>day|hour|tag|stunde)\b|"
    r"(?P<amount_after>\d{1,5}(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency_after>€|eur|usd|\$|gbp|£)\s*"
    r"(?:/|per\s+|pro\s+)(?P<period_after>day|hour|tag|stunde)\b",
    re.IGNORECASE,
)
_RATE_FREELANCE_CONTEXT_RE = re.compile(
    r"freelanc|freiberuf|selbstst(?:ä|ae)ndig|\bb2b\b|"
    r"self[\s-]+employed|independent[\s-]+contractor|contractor\s+basis",
    re.IGNORECASE,
)

_RELATIONSHIP_LABELS = {
    "employee": "Employee",
    "contract_employee": "Contract employee",
    "freelance": "Freelance",
    "working_student": "Working student",
    "internship": "Internship",
}
_SCHEDULE_LABELS = {"full_time": "Full-time", "part_time": "Part-time"}
_TERM_LABELS = {"permanent": "Permanent", "fixed_term": "Fixed-term"}


def _structured_value(structured: StructuredEmployment | None, key: str) -> object | None:
    if structured is None:
        return None
    if isinstance(structured, Mapping):
        return structured.get(key)
    return getattr(structured, key, None)


def _normalized_literal(value: object, supported: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in supported else None


def _normalized_hours(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 168 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if 1 <= parsed <= 168 else None
    return None


def _bounded_detail(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized[:MAX_EMPLOYMENT_DETAIL_LENGTH] or None


def _add_reason(reasons: list[str], reason: str) -> None:
    normalized = " ".join(reason.split())[:MAX_EMPLOYMENT_REASON_LENGTH]
    if normalized and normalized not in reasons and len(reasons) < MAX_EMPLOYMENT_REASONS:
        reasons.append(normalized)


def _has_dimension_reason(reasons: list[str], dimension: str, value: object) -> bool:
    suffix = f"={value}"
    return any(
        f":{dimension}=" in reason
        or (reason.startswith("structured:") and reason.endswith(suffix))
        for reason in reasons
    )


def merge_structured_employment_inputs(
    *inputs: EmploymentStructuredInput,
) -> EmploymentStructuredInput:
    """Merge provider field mappings independently, dropping conflicts.

    A source can expose multiple structured labels for one listing (for
    example Greenhouse custom metadata or JSON-LD arrays). Agreement is kept;
    conflicting normalized values leave only that dimension unsupported so
    normal heuristics can fill it later.
    """

    merged: dict[str, object | None] = {}
    for dimension in _STRUCTURED_DIMENSIONS:
        values = {
            getattr(value, dimension)
            for value in inputs
            if getattr(value, dimension) is not None
        }
        merged[dimension] = next(iter(values)) if len(values) == 1 else None
    return EmploymentStructuredInput(**merged)  # type: ignore[arg-type]


def _reason_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:60].strip("_")


def _relationship_signals(text: str) -> set[str]:
    signals: set[str] = set()
    if _WORKING_STUDENT_RE.search(text):
        signals.add("working_student")
    if _INTERNSHIP_RE.search(text):
        signals.add("internship")
    if _CONTRACT_EMPLOYEE_RE.search(text):
        signals.add("contract_employee")
    if _FREELANCE_RE.search(text) or _extract_rate(text) is not None:
        signals.add("freelance")
    if _EMPLOYEE_RE.search(text):
        signals.add("employee")
    return signals


def _resolve_relationship(text: str) -> tuple[str | None, bool]:
    signals = _relationship_signals(text)
    if not signals:
        return None, False
    specific = signals - {"employee"}
    if len(specific) == 1:
        return next(iter(specific)), False
    if len(specific) > 1:
        return None, True
    return "employee", False


def _resolve_binary_labels(
    text: str,
    first_pattern: re.Pattern[str],
    first_value: str,
    second_pattern: re.Pattern[str],
    second_value: str,
) -> tuple[str | None, bool]:
    first = bool(first_pattern.search(text))
    second = bool(second_pattern.search(text))
    if first and second:
        return None, True
    if first:
        return first_value, False
    if second:
        return second_value, False
    return None, False


def _hours_matches(text: str, scope: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _HOURS_RANGE_RE.finditer(text):
        low = int(match.group("low"))
        high = int(match.group("high"))
        if not (1 <= low <= high <= 168):
            continue
        found.append((high, f"range={low}-{high}h/week"))
        occupied.append(match.span())
    for match in _HOURS_SINGLE_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        hours = int(match.group("hours"))
        if 1 <= hours <= 168:
            found.append((hours, f"value={hours}h/week"))
            occupied.append(match.span())
    for match in _HOURS_COMPACT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        hours = int(match.group("hours"))
        if not 1 <= hours <= 168:
            continue
        prefix = text[max(0, match.start() - 20):match.start()]
        surrounding = text[max(0, match.start() - 35):match.end() + 35]
        parenthesized = bool(
            re.search(rf"\(\s*{hours}\s*h\s*\)", surrounding, re.IGNORECASE)
        )
        contextual = bool(_COMPACT_HOURS_CONTEXT_RE.search(surrounding))
        title_context = scope == "title_tags" and bool(_TITLE_ROLE_CONTEXT_RE.search(text))
        if _COMPACT_HOURS_ID_RE.search(prefix):
            continue
        if parenthesized or contextual or title_context:
            found.append((hours, f"value={hours}h/week"))
    return found


def extract_weekly_hours(text: str, *, scope: str = "description") -> tuple[int | None, str | None]:
    """Extract context-bound weekly hours, retaining a stable evidence code."""

    matches = _hours_matches(text, scope)
    if not matches:
        return None, None
    values = {value for value, _reason in matches}
    if len(values) != 1:
        return None, "conflict"
    value = matches[0][0]
    range_reason = next((reason for _value, reason in matches if reason.startswith("range=")), None)
    return value, range_reason or f"value={value}h/week"


def _normalize_duration(count: str, unit: str) -> str:
    normalized_unit = unit.lower()
    if normalized_unit.startswith("monat"):
        normalized_unit = "month"
    elif normalized_unit.startswith("woch"):
        normalized_unit = "week"
    elif normalized_unit.startswith("jahr"):
        normalized_unit = "year"
    else:
        normalized_unit = normalized_unit.rstrip("s")
    suffix = "" if int(count) == 1 else "s"
    return f"{int(count)} {normalized_unit}{suffix}"


def _extract_duration(text: str) -> str | None:
    values: list[str] = []
    for pattern in _DURATION_PATTERNS:
        for match in pattern.finditer(text):
            count = int(match.group("count"))
            if 1 <= count <= 168:
                values.append(_normalize_duration(match.group("count"), match.group("unit")))
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def _currency(value: str | None) -> str:
    if not value:
        return ""
    return {"eur": "EUR", "usd": "USD", "gbp": "GBP"}.get(value.lower(), value)


def _format_rate(currency: str, amount: str, period: str) -> str:
    if currency in {"€", "$", "£"}:
        return f"{currency}{amount}/{period}"
    if currency:
        return f"{amount} {currency}/{period}"
    return f"{amount}/{period}"


def _extract_rate(text: str) -> str | None:
    label_match = _RATE_LABEL_RE.search(text)
    if label_match:
        currency = _currency(
            label_match.group("currency_before") or label_match.group("currency_after")
        )
        amount = label_match.group("amount")
        label = label_match.group("label").lower()
        period = "day" if "day" in label or "tagessatz" in label else "hour"
        return _format_rate(currency, amount, period)

    for match in _RATE_SLASH_RE.finditer(text):
        currency = _currency(match.group("currency_before") or match.group("currency_after"))
        amount = match.group("amount") or match.group("amount_after")
        period_raw = (match.group("period") or match.group("period_after") or "").lower()
        period = "day" if period_raw in {"day", "tag"} else "hour"
        surrounding = text[max(0, match.start() - 80):match.end() + 80]
        if period == "hour" and not _RATE_FREELANCE_CONTEXT_RE.search(surrounding):
            continue
        return _format_rate(currency, amount, period)
    return None


def _heuristic_from_scopes(
    title_tags: str,
    description: str,
    resolver,
) -> tuple[str | None, str | None, bool]:
    for scope, text in (("title_tags", title_tags), ("description", description)):
        value, conflict = resolver(text)
        if value is not None or conflict:
            return value, scope, conflict
    return None, None, False


def classify_employment(
    job: Job,
    structured: StructuredEmployment | None = None,
    *,
    structured_source: str | None = None,
    structured_fields: Mapping[str, str] | None = None,
) -> Job:
    """Classify each employment dimension without changing unrelated fields."""

    reasons = list(job.employment_reasons)
    title_tags = " ".join((job.title, *job.tags))
    description = job.description or ""

    def structured_or_job(key: str, current: object) -> object:
        supplied = _structured_value(structured, key)
        return current if supplied is None else supplied

    relationship = _normalized_literal(
        structured_or_job("employment_relationship", job.employment_relationship),
        EMPLOYMENT_RELATIONSHIPS,
    )
    relationship = relationship if relationship != "unknown" else None
    schedule = _normalized_literal(
        structured_or_job("work_schedule", job.work_schedule), WORK_SCHEDULES
    )
    schedule = schedule if schedule != "unknown" else None
    term = _normalized_literal(
        structured_or_job("contract_term", job.contract_term), CONTRACT_TERMS
    )
    term = term if term != "unknown" else None
    hours = _normalized_hours(structured_or_job("weekly_hours", job.weekly_hours))
    duration = _bounded_detail(
        structured_or_job("contract_duration", job.contract_duration)
    )
    rate = _bounded_detail(structured_or_job("freelance_rate", job.freelance_rate))

    structured_dimensions = {
        "relationship": relationship is not None,
        "schedule": schedule is not None,
        "term": term is not None,
        "hours": hours is not None,
        "duration": duration is not None,
        "rate": rate is not None,
    }
    for model_dimension, reason_dimension, value in (
        ("employment_relationship", "relationship", relationship),
        ("work_schedule", "schedule", schedule),
        ("contract_term", "term", term),
        ("weekly_hours", "hours", hours),
        ("contract_duration", "duration", duration),
        ("freelance_rate", "rate", rate),
    ):
        if value is None or _has_dimension_reason(reasons, reason_dimension, value):
            continue
        source = _reason_token(structured_source or "")
        field = _reason_token((structured_fields or {}).get(model_dimension, ""))
        if source and field:
            _add_reason(reasons, f"structured:{source}:{field}={value}")
        else:
            _add_reason(reasons, f"structured:{reason_dimension}={value}")

    if relationship is None:
        value, scope, conflict = _heuristic_from_scopes(
            title_tags, description, _resolve_relationship
        )
        if conflict:
            _add_reason(reasons, f"heuristic:{scope}:relationship=conflict")
        elif value is not None:
            relationship = value
            _add_reason(reasons, f"heuristic:{scope}:relationship={value}")

    if schedule is None:
        value, scope, conflict = _heuristic_from_scopes(
            title_tags,
            description,
            lambda text: _resolve_binary_labels(
                text, _FULL_TIME_RE, "full_time", _PART_TIME_RE, "part_time"
            ),
        )
        if conflict:
            _add_reason(reasons, f"heuristic:{scope}:schedule=conflict")
        elif value is not None:
            schedule = value
            _add_reason(reasons, f"heuristic:{scope}:schedule={value}")

    if hours is None:
        for scope, text in (("title_tags", title_tags), ("description", description)):
            value, evidence = extract_weekly_hours(text, scope=scope)
            if evidence is not None:
                if value is None:
                    _add_reason(reasons, f"heuristic:{scope}:hours=conflict")
                else:
                    hours = value
                    _add_reason(reasons, f"heuristic:{scope}:hours:{evidence}")
                break

    if schedule is None and hours is not None and hours <= 32:
        schedule = "part_time"
        _add_reason(reasons, "heuristic:hours:schedule=part_time")

    if term is None:
        def resolve_term(text: str) -> tuple[str | None, bool]:
            explicit, conflict = _resolve_binary_labels(
                text, _PERMANENT_RE, "permanent", _FIXED_TERM_RE, "fixed_term"
            )
            if explicit is not None or conflict:
                return explicit, conflict
            return ("fixed_term", False) if _extract_duration(text) else (None, False)

        value, scope, conflict = _heuristic_from_scopes(
            title_tags, description, resolve_term
        )
        if conflict:
            _add_reason(reasons, f"heuristic:{scope}:term=conflict")
        elif value is not None:
            term = value
            _add_reason(reasons, f"heuristic:{scope}:term={value}")

    if duration is None and term != "permanent":
        for scope, text in (("title_tags", title_tags), ("description", description)):
            value = _extract_duration(text)
            if value:
                duration = value
                _add_reason(reasons, f"heuristic:{scope}:duration={value}")
                break

    if rate is None and relationship in {None, "freelance"}:
        for scope, text in (("title_tags", title_tags), ("description", description)):
            value = _extract_rate(text)
            if value:
                rate = value
                _add_reason(reasons, f"heuristic:{scope}:rate={value}")
                if relationship is None:
                    relationship = "freelance"
                    _add_reason(reasons, f"heuristic:{scope}:relationship=freelance")
                break

    # A structured freelance rate is positive freelance evidence when the
    # relationship itself remains unknown, but never overwrites a relationship.
    if relationship is None and structured_dimensions["rate"]:
        relationship = "freelance"
        _add_reason(reasons, "structured:rate:relationship=freelance")

    job.employment_relationship = relationship or "unknown"
    job.work_schedule = schedule or "unknown"
    job.contract_term = term or "unknown"
    job.weekly_hours = hours
    job.contract_duration = duration
    job.freelance_rate = rate
    job.employment_reasons = reasons[:MAX_EMPLOYMENT_REASONS]
    return job


def employment_rejection_reason(
    job: Job,
    policy: EmploymentPolicy | None = None,
) -> str | None:
    """Apply the one profile-driven employment gate and derive its marker."""

    current_policy = policy or load_employment_policy()
    job.freelance_permission_required = bool(
        job.employment_relationship == "freelance"
        and current_policy.freelance_permission_required
    )

    if job.weekly_hours is not None:
        if (
            current_policy.preferred_weekly_hours_min is not None
            and job.weekly_hours < current_policy.preferred_weekly_hours_min
        ):
            _add_reason(job.employment_reasons, "policy:weekly_hours_below_preference")
        if (
            current_policy.preferred_weekly_hours_max is not None
            and job.weekly_hours > current_policy.preferred_weekly_hours_max
        ):
            _add_reason(job.employment_reasons, "policy:weekly_hours_above_preference")

    relationship = job.employment_relationship
    if relationship in current_policy.rejected_relationships:
        return f"employment relationship '{relationship}' is rejected by profile"
    if relationship not in current_policy.accepted_relationships:
        return f"employment relationship '{relationship}' is not accepted by profile"
    if job.work_schedule not in current_policy.accepted_schedules:
        return f"work schedule '{job.work_schedule}' is not accepted by profile"
    return None


def passes_employment_filter(
    job: Job,
    policy: EmploymentPolicy | None = None,
) -> bool:
    return employment_rejection_reason(job, policy) is None


def _employment_display_parts(
    relationship_value: object,
    schedule_value: object,
    term_value: object,
    weekly_hours: object,
    contract_duration: object,
    freelance_rate: object,
) -> list[str]:
    parts: list[str] = []
    relationship = _RELATIONSHIP_LABELS.get(str(relationship_value))
    schedule = _SCHEDULE_LABELS.get(str(schedule_value))
    term = _TERM_LABELS.get(str(term_value))
    for value in (relationship, schedule, term):
        if value:
            parts.append(value)
    if isinstance(weekly_hours, int) and not isinstance(weekly_hours, bool):
        parts.append(f"{weekly_hours}h/week")
    for value in (contract_duration, freelance_rate):
        if isinstance(value, str) and value:
            parts.append(value[:MAX_EMPLOYMENT_DETAIL_LENGTH])
    return parts


def employment_display_parts(job: Job) -> list[str]:
    """Return compact human-readable known employment fields."""

    return _employment_display_parts(
        job.employment_relationship,
        job.work_schedule,
        job.contract_term,
        job.weekly_hours,
        job.contract_duration,
        job.freelance_rate,
    )


def employment_display_lines(job: Job) -> list[str]:
    parts = employment_display_parts(job)
    lines = [f"💼 {' · '.join(parts)}"] if parts else []
    if job.freelance_permission_required:
        lines.append("⚠️ Freelance permission required")
    return lines


def persisted_employment_display_lines(row: Mapping[str, object]) -> list[str]:
    """Format employment fields from a persisted digest row."""

    parts = _employment_display_parts(
        row.get("employment_relationship", "unknown"),
        row.get("work_schedule", "unknown"),
        row.get("contract_term", "unknown"),
        row.get("weekly_hours"),
        row.get("contract_duration"),
        row.get("freelance_rate"),
    )
    lines = [f"💼 {' · '.join(parts)}"] if parts else []
    if bool(row.get("freelance_permission_required", False)):
        lines.append("⚠️ Freelance permission required")
    return lines
