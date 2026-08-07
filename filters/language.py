"""Requirement-aware German language evaluation and posting enrichment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException
from loguru import logger

from filters.profile import LanguagePolicy, load_language_policy
from models.job import (
    GermanRequirementLevel,
    GermanRequirementStatus,
    Job,
    MAX_LANGUAGE_REASON_LENGTH,
    MAX_LANGUAGE_REASONS,
    PostingLanguage,
)

# Keep detection deterministic across processes and test runs.
DetectorFactory.seed = 0

_CEFR_ORDER = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}
_LEVEL_RANK = {
    "none": 0,
    "unknown": 0,
    "a1": 10,
    "a2": 20,
    "b1": 30,
    "b2": 40,
    "fluent": 45,
    "c1": 50,
    "business_fluent": 55,
    "c2": 60,
    "native": 70,
}

_GERMAN_RE = re.compile(
    r"\b(?:german|deutsch(?:e|en|er|es)?|deutschkenntnisse(?:n)?|"
    r"deutschsprach(?:ig|ige|igen|iger|iges)?)\b",
    re.IGNORECASE,
)
_OTHER_LANGUAGE_RE = re.compile(
    r"\b(?:english|englisch|french|franz(?:ö|oe)sisch|spanish|spanisch|"
    r"italian|italienisch|dutch|niederl(?:ä|ae)ndisch)\b",
    re.IGNORECASE,
)
_ENGLISH_RE = re.compile(r"\b(?:english|englisch)\b", re.IGNORECASE)
_LEVEL_RE = re.compile(r"\b([abc][12])\b", re.IGNORECASE)
_BUSINESS_FLUENT_RE = re.compile(
    r"\b(?:business[- ]fluent|professional\s+working\s+proficiency|"
    r"verhandlungssicher\w*)\b",
    re.IGNORECASE,
)
_NATIVE_RE = re.compile(
    r"\b(?:native(?:[- ]level)?|mother\s+tongue|muttersprache|"
    r"muttersprachlich\w*)\b",
    re.IGNORECASE,
)
_FLUENT_RE = re.compile(r"\bfluent\b", re.IGNORECASE)
_EXPLICIT_PROFICIENCY_RE = re.compile(
    r"\b(?:[abc][12]|fluent|business[- ]fluent|native(?:[- ]level)?|"
    r"professional\s+working\s+proficiency|mother\s+tongue)\b",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    r"(?:\b(?:no|without)\s+(?:german|deutschkenntnisse)\b|"
    r"\b(?:german|deutsch(?:kenntnisse)?)\b.{0,45}\b(?:not\s+required|"
    r"not\s+necessary|isn['’]?t\s+required|nicht\s+erforderlich|"
    r"keine\s+voraussetzung)\b|"
    r"\bkeine\s+deutschkenntnisse\b.{0,30}\b(?:erforderlich|notwendig)|"
    r"\bdeutschkenntnisse\b.{0,35}\bsind\s+keine\s+voraussetzung\b)",
    re.IGNORECASE,
)
_OPTIONAL_RE = re.compile(
    r"\b(?:optional|preferred|nice[- ]to[- ]have|nice\s+to\s+have|"
    r"a\s+plus|pluspunkt|advantageous|beneficial|desirable|helpful|"
    r"would\s+be\s+(?:a\s+)?(?:plus|helpful|nice)|w(?:ü|ue)nschenswert|"
    r"von\s+vorteil)\b",
    re.IGNORECASE,
)
_REQUIRED_RE = re.compile(
    r"\b(?:required|must\s+have|minimum|at\s+least|you\s+need|we\s+require|"
    r"mandatory|erforderlich|vorausgesetzt|mindestens|zwingend|voraussetzung)\b",
    re.IGNORECASE,
)
_FUTURE_RE = re.compile(
    r"\b(?:willing(?:ness)?\s+to\s+(?:learn|improve)|aim\s+to\s+reach|"
    r"working\s+towards?|future\s+target|develop\s+(?:your\s+)?german)\b",
    re.IGNORECASE,
)
_IRRELEVANT_RE = re.compile(
    r"(?:\b(?:course|training|lessons?|classes|certification)\b.{0,80}"
    r"\b(?:provided|offered|available|included|support(?:ed)?)\b|"
    r"\b(?:provide|offer|include|support)\w*\b.{0,80}"
    r"\b(?:course|training|lessons?|classes|certification)\b)",
    re.IGNORECASE,
)
_NON_REQUIREMENT_GERMAN_RE = re.compile(
    r"\b(?:german\s+(?:market|office|company|customers?|clients?|team|law|"
    r"regulations?)|deutsch(?:er|en|e|es)?\s+(?:markt|b(?:ü|ue)ro|"
    r"unternehmen|kunden?|recht))\b",
    re.IGNORECASE,
)
_HIRING_LANGUAGE_RE = re.compile(
    r"\b(?:language|proficiency|skills?|knowledge|kenntnisse|sprachkenntnisse|"
    r"niveau|level|fluent|native|muttersprache|verhandlungssicher)\b",
    re.IGNORECASE,
)
_CONNECTOR_RE = re.compile(r"\b(or|oder|and|und)\b", re.IGNORECASE)
_HTML_BOUNDARY_RE = re.compile(
    r"</?(?:li|p|br|div|h[1-6]|ul|ol)[^>]*>", re.IGNORECASE
)

EvidenceContext = Literal["required", "optional", "not_required", "ambiguous"]


@dataclass(frozen=True)
class _Evidence:
    level: GermanRequirementLevel
    context: EvidenceContext

    @property
    def reason(self) -> str:
        return f"german_requirement={self.level}:{self.context}"


@dataclass(frozen=True)
class _ClauseDecision:
    status: GermanRequirementStatus
    level: GermanRequirementLevel


def detect_posting_language(job: Job) -> PostingLanguage:
    """Detect advertisement prose language from the approved bounded sample."""

    sample = job.title
    if job.description:
        sample += " " + job.description[:300]
    if len(sample.strip()) < 20:
        return "unknown"
    try:
        language = detect(sample)
    except LangDetectException:
        return "unknown"
    if language == "en":
        return "en"
    if language == "de":
        return "de"
    return "other"


def _distance(span: tuple[int, int], other: tuple[int, int]) -> int:
    if span[1] < other[0]:
        return other[0] - span[1]
    if other[1] < span[0]:
        return span[0] - other[1]
    return 0


def _nearest_distance(pattern: re.Pattern[str], text: str, span: tuple[int, int]) -> int | None:
    distances = [_distance(span, match.span()) for match in pattern.finditer(text)]
    return min(distances) if distances else None


def _associated_with_german(
    text: str,
    span: tuple[int, int],
    *,
    carry_german: bool = False,
) -> bool:
    german_distance = _nearest_distance(_GERMAN_RE, text, span)
    if german_distance is None:
        return carry_german and _nearest_distance(_OTHER_LANGUAGE_RE, text, span) is None
    if german_distance > 90:
        return False
    other_distance = _nearest_distance(_OTHER_LANGUAGE_RE, text, span)
    return other_distance is None or german_distance <= other_distance


def _candidate_matches(
    text: str,
    *,
    carry_german: bool = False,
) -> list[tuple[GermanRequirementLevel, tuple[int, int]]]:
    matches: list[tuple[GermanRequirementLevel, tuple[int, int]]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, level in (
        (_BUSINESS_FLUENT_RE, "business_fluent"),
        (_NATIVE_RE, "native"),
        (_FLUENT_RE, "fluent"),
    ):
        for match in pattern.finditer(text):
            span = match.span()
            if any(_distance(span, used) == 0 for used in occupied):
                continue
            if not _associated_with_german(text, span, carry_german=carry_german):
                continue
            matches.append((cast(GermanRequirementLevel, level), span))
            occupied.append(span)
    for match in _LEVEL_RE.finditer(text):
        span = match.span()
        if _associated_with_german(text, span, carry_german=carry_german):
            matches.append((cast(GermanRequirementLevel, match.group(1).lower()), span))
    return sorted(matches, key=lambda item: item[1][0])


def _cue_context(text: str, span: tuple[int, int]) -> EvidenceContext:
    future_distance = _nearest_distance(_FUTURE_RE, text, span)
    optional_distance = _nearest_distance(_OPTIONAL_RE, text, span)
    required_distance = _nearest_distance(_REQUIRED_RE, text, span)
    if future_distance is not None and future_distance <= 90:
        return "ambiguous"
    if optional_distance is not None and optional_distance <= 90:
        if required_distance is None or optional_distance <= required_distance:
            return "optional"
    if required_distance is not None and required_distance <= 90:
        return "required"
    # Explicit CEFR levels and strong descriptors are direct qualifications.
    return "required"


def _extract_segment_evidence(
    text: str,
    *,
    carry_german: bool = False,
) -> list[_Evidence]:
    if carry_german and _OTHER_LANGUAGE_RE.search(text):
        carry_german = False
    has_german = bool(_GERMAN_RE.search(text)) or carry_german
    if not has_german:
        return []
    if _IRRELEVANT_RE.search(text):
        return []
    if (
        _NON_REQUIREMENT_GERMAN_RE.search(text)
        and not _HIRING_LANGUAGE_RE.search(text)
        and not _LEVEL_RE.search(text)
        and not _REQUIRED_RE.search(text)
        and not _OPTIONAL_RE.search(text)
    ):
        return []

    candidates = _candidate_matches(text, carry_german=carry_german)
    negated = bool(_NEGATION_RE.search(text))
    if negated and candidates and _OPTIONAL_RE.search(text):
        return [_Evidence(level, "optional") for level, _ in candidates]
    if negated:
        return [_Evidence("none", "not_required")]
    if candidates:
        return [_Evidence(level, _cue_context(text, span)) for level, span in candidates]
    if _FUTURE_RE.search(text):
        return [_Evidence("unknown", "ambiguous")]
    if _OPTIONAL_RE.search(text):
        return [_Evidence("unknown", "optional")]
    if _REQUIRED_RE.search(text):
        return [_Evidence("unknown", "required")]
    return [_Evidence("unknown", "ambiguous")]


def _strongest(evidence: list[_Evidence]) -> GermanRequirementLevel:
    if not evidence:
        return "unknown"
    return max(evidence, key=lambda item: _LEVEL_RANK[item.level]).level


def _requirement_status(
    level: GermanRequirementLevel,
    policy: LanguagePolicy,
) -> GermanRequirementStatus:
    if level == "native":
        return "incompatible"
    minimum = {
        "fluent": "b2",
        "business_fluent": "c1",
    }.get(level, level)
    if minimum == "unknown":
        return "compatible"
    if minimum == "none":
        return "optional"
    return cast(
        GermanRequirementStatus,
        "compatible"
        if _CEFR_ORDER[minimum] <= _CEFR_ORDER[policy.max_german_level]
        else "incompatible",
    )


def _english_is_accepted(policy: LanguagePolicy) -> bool:
    return bool({"en", "english"} & policy.accepted_languages)


def _alternative_decision(
    text: str,
    policy: LanguagePolicy,
) -> tuple[list[_Evidence], _ClauseDecision, str | None] | None:
    for connector in _CONNECTOR_RE.finditer(text):
        left = text[: connector.start()]
        right = text[connector.end() :]
        left_german = bool(_GERMAN_RE.search(left))
        right_german = bool(_GERMAN_RE.search(right))
        if left_german == right_german:
            continue
        german_branch, other_branch = (left, right) if left_german else (right, left)
        if not _ENGLISH_RE.search(other_branch):
            continue
        evidence = _extract_segment_evidence(german_branch)
        required = [item for item in evidence if item.context == "required"]
        if not required:
            return None
        level = _strongest(required)
        german_status = _requirement_status(level, policy)
        connector_value = connector.group(1).lower()
        if connector_value in {"and", "und"}:
            return evidence, _ClauseDecision(german_status, level), None
        if german_status == "compatible":
            return evidence, _ClauseDecision("compatible", level), None
        if not _english_is_accepted(policy):
            return evidence, _ClauseDecision("incompatible", level), None
        if _EXPLICIT_PROFICIENCY_RE.search(other_branch):
            reason = "alternative_language_requirement=english_explicit_level_unmodeled"
            return evidence, _ClauseDecision("unknown", level), reason
        reason = "alternative_language_requirement=english_unrestricted_accepted"
        return evidence, _ClauseDecision("compatible", level), reason
    return None


def _language_windows(job: Job) -> list[tuple[str, bool]]:
    text = ". ".join(
        value for value in (job.title, " ".join(job.tags), job.description or "") if value
    )
    text = _HTML_BOUNDARY_RE.sub("; ", text)
    text = " ".join(text.split())
    windows: list[tuple[str, bool]] = []
    carry_german = False
    position = 0
    for boundary in re.finditer(r"[;.!?]+|$", text):
        segment = text[position : boundary.start()].strip()
        separator = boundary.group(0)
        position = boundary.end()
        if not segment:
            carry_german = separator == ";" and carry_german
            if not separator:
                break
            continue
        has_german = bool(_GERMAN_RE.search(segment))
        can_carry = carry_german and bool(
            _LEVEL_RE.search(segment)
            or _BUSINESS_FLUENT_RE.search(segment)
            or _NATIVE_RE.search(segment)
            or _FLUENT_RE.search(segment)
        )
        if has_german or can_carry:
            if len(segment) <= 500:
                windows.append((segment, can_carry and not has_german))
            else:
                for match in _GERMAN_RE.finditer(segment):
                    start = max(0, match.start() - 220)
                    end = min(len(segment), match.end() + 220)
                    windows.append((segment[start:end], False))
        carry_german = separator == ";" and (has_german or can_carry)
        if not separator:
            break
    # Preserve order while removing repeated windows from nearby German terms.
    unique: list[tuple[str, bool]] = []
    for item in windows:
        if item not in unique:
            unique.append(item)
    return unique


def _bounded_reasons(values: list[str]) -> list[str]:
    reasons: list[str] = []
    for value in values:
        reason = " ".join(value.split())[:MAX_LANGUAGE_REASON_LENGTH]
        if reason and reason not in reasons:
            reasons.append(reason)
        if len(reasons) >= MAX_LANGUAGE_REASONS:
            break
    return reasons


def evaluate_language(job: Job, policy: LanguagePolicy) -> bool:
    """Populate normalized language metadata and return the gate decision."""

    job.posting_language = detect_posting_language(job)
    evidence: list[_Evidence] = []
    independent_evidence: list[_Evidence] = []
    clause_decisions: list[_ClauseDecision] = []
    extra_reasons: list[str] = []
    for segment, carry_german in _language_windows(job):
        alternative = _alternative_decision(segment, policy)
        if alternative is not None:
            segment_evidence, decision, reason = alternative
            evidence.extend(segment_evidence)
            clause_decisions.append(decision)
            if reason:
                extra_reasons.append(reason)
            continue
        segment_evidence = _extract_segment_evidence(
            segment, carry_german=carry_german
        )
        evidence.extend(segment_evidence)
        independent_evidence.extend(segment_evidence)

    required = [
        item for item in independent_evidence if item.context == "required"
    ]
    optional = [item for item in evidence if item.context == "optional"]
    negated = [item for item in evidence if item.context == "not_required"]
    ambiguous = [item for item in evidence if item.context == "ambiguous"]
    if required:
        level = _strongest(required)
        clause_decisions.append(_ClauseDecision(_requirement_status(level, policy), level))

    if negated and clause_decisions:
        status: GermanRequirementStatus = "unknown"
        selected_level = _strongest(required) if required else clause_decisions[0].level
        extra_reasons.append("german_requirement=conflicting:ambiguous")
    elif clause_decisions:
        selected_level = max(
            clause_decisions, key=lambda item: _LEVEL_RANK[item.level]
        ).level
        statuses = {item.status for item in clause_decisions}
        if "incompatible" in statuses:
            status = "incompatible"
        elif "unknown" in statuses:
            status = "unknown"
        else:
            status = "compatible"
    elif optional:
        status = "optional"
        selected_level = _strongest(optional)
    elif negated:
        status = "optional"
        selected_level = "none"
    elif ambiguous:
        status = "unknown"
        selected_level = _strongest(ambiguous)
    else:
        status = "unspecified"
        selected_level = "unknown"

    job.german_requirement_status = status
    job.german_requirement_level = selected_level
    job.language_reasons = _bounded_reasons(
        [f"posting_language={job.posting_language}"]
        + [item.reason for item in evidence]
        + extra_reasons
    )
    passes = status != "incompatible"
    logger.debug(
        "Language {} (posting={}, German={} {}): {}",
        "ACCEPT" if passes else "REJECT",
        job.posting_language,
        selected_level,
        status,
        job.title,
    )
    return passes


def passes_language_filter(job: Job) -> bool:
    """Backward-compatible language-gate interface for existing callers."""

    return evaluate_language(job, load_language_policy())


_LEVEL_LABELS = {
    "a1": "A1",
    "a2": "A2",
    "b1": "B1",
    "b2": "B2",
    "c1": "C1",
    "c2": "C2",
    "fluent": "fluent",
    "business_fluent": "business-fluent",
    "native": "native",
    "unknown": "",
    "none": "",
}


def language_display_text(
    job: Job,
    *,
    policy: LanguagePolicy | None = None,
    include_evidence: bool = False,
) -> str | None:
    """Return one compact CLI/explain line, omitting ordinary English jobs."""

    status = job.german_requirement_status
    level = _LEVEL_LABELS[job.german_requirement_level]
    if status == "unspecified":
        if job.posting_language == "de":
            text = "Language: German posting · German requirement unspecified"
        else:
            return None
    elif status == "optional":
        text = (
            f"Language: German {level} preferred"
            if level
            else "Language: German not required"
        )
    elif status == "unknown":
        if (
            "alternative_language_requirement=english_explicit_level_unmodeled"
            in job.language_reasons
        ):
            level_text = f" {level}" if level else ""
            text = (
                f"Language: German{level_text} or explicit English proficiency "
                "requirement; compatibility uncertain"
            )
        else:
            text = "Language: German requirement uncertain"
    else:
        level_text = f" {level}" if level else ""
        text = f"Language: German{level_text} required"
        if status == "incompatible":
            maximum = (policy or load_language_policy()).max_german_level.upper()
            text += f"; candidate max {maximum}"
    if include_evidence and job.language_reasons:
        text += " · " + "; ".join(job.language_reasons[:4])
    return text


def language_rejection_explanation(job: Job, policy: LanguagePolicy) -> str:
    """Return the stable human-readable terminal language explanation."""

    return language_display_text(job, policy=policy) or "Language: incompatible"
