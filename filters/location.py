"""Strict Germany eligibility and Berlin workplace evaluation.

Remote jobs must explicitly allow Germany, a region containing Germany, or
worldwide work. Hybrid/on-site jobs must have a Berlin workplace. Unknown
eligibility is deliberately rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from models.job import Job

GERMANY_TOKENS = {"germany", "deutschland"}
STRUCTURED_GERMANY_TOKENS = GERMANY_TOKENS | {"de"}
BERLIN_TOKENS = {"berlin"}
BROAD_REGIONS = {
    "worldwide", "global", "anywhere", "europe", "european union",
    "eu", "eea", "emea", "dach",
}
NON_GERMANY_COUNTRIES = {
    "france", "spain", "poland", "portugal", "italy", "netherlands",
    "belgium", "austria", "switzerland", "ireland", "sweden", "denmark",
    "norway", "finland", "czech republic", "czechia", "romania",
    "hungary", "bulgaria", "croatia", "greece", "united kingdom", "uk",
    "united states", "usa", "us", "canada", "australia", "india",
    "nantes", "paris", "lyon", "madrid", "barcelona", "lisbon", "warsaw",
    "wrocław", "wroclaw", "kraków", "krakow", "bucharest", "london",
}
NON_GERMANY_CODES = {
    "fr", "es", "pl", "pt", "it", "nl", "be", "at", "ch", "ie", "se",
    "dk", "no", "fi", "cz", "ro", "hu", "bg", "hr", "gr", "gb", "uk",
    "us", "ca", "au", "in",
}
REMOTE_SIGNALS = (
    "remote", "work from home", "home office", "homeoffice", "distributed",
    "telecommute", "work from anywhere",
)
HYBRID_SIGNALS = ("hybrid", "partly remote", "partially remote")
ONSITE_SIGNALS = (
    "on-site", "onsite", "on site", "in-office", "in office", "office-based",
)
RESTRICTION_RE = re.compile(
    r"(?:must|need to|should)\s+(?:be\s+)?(?:based|located|resident|reside|live)"
    r"\s+(?:in|within)|"
    r"(?:candidates?|applicants?|employees?)\s+(?:must\s+)?(?:be\s+)?"
    r"(?:based|located|resident|residing|living)\s+(?:in|within)|"
    r"(?:remote|remotely|working\s+remotely)\s+(?:only\s+)?(?:from|in)|"
    r"work\s+from|"
    r"(?:only|exclusively)\s+(?:in|within)|eligible\s+to\s+work\s+in",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    scope: str
    workplace_type: str
    reasons: list[str]


def _contains_token(text: str, tokens: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(token)}\b", text) for token in tokens)


def _restriction_windows(description: str) -> list[str]:
    text = description.lower()
    return [text[m.start():m.start() + 180] for m in RESTRICTION_RE.finditer(text)]


def infer_workplace_type(job: Job) -> str:
    if job.workplace_type != "unknown":
        return job.workplace_type
    text = f"{job.location} {' '.join(job.tags)} {(job.description or '')[:1000]}".lower()
    if any(signal in text for signal in HYBRID_SIGNALS):
        return "hybrid"
    if any(signal in text for signal in ONSITE_SIGNALS):
        return "onsite"
    if job.is_remote or any(signal in text for signal in REMOTE_SIGNALS):
        return "remote"
    return "unknown"


def _structured_remote_decision(job: Job) -> tuple[bool | None, str | None]:
    countries = {value.lower() for value in job.eligible_countries}
    regions = {value.lower() for value in job.eligible_regions}
    if countries:
        if countries & STRUCTURED_GERMANY_TOKENS:
            return True, "structured eligibility includes Germany"
        return False, f"structured eligibility is limited to {', '.join(sorted(countries))}"
    if regions:
        if any(_contains_token(region, BROAD_REGIONS) for region in regions):
            return True, f"structured eligibility includes {', '.join(sorted(regions))}"
        return False, f"structured eligibility excludes Germany: {', '.join(sorted(regions))}"
    return None, None


def evaluate_eligibility(job: Job) -> EligibilityResult:
    workplace = infer_workplace_type(job)
    loc = (job.location or "").lower()
    windows = _restriction_windows(job.description or "")

    if "excluding germany" in (job.description or "").lower() or "except germany" in (job.description or "").lower():
        return EligibilityResult(False, "restricted", workplace, ["posting explicitly excludes Germany"])

    if workplace in ("hybrid", "onsite"):
        if _contains_token(loc, BERLIN_TOKENS):
            return EligibilityResult(
                True, "berlin", workplace,
                [f"{workplace} workplace is in Berlin"],
            )
        return EligibilityResult(
            False, "restricted", workplace,
            [f"{workplace} roles are accepted only in Berlin"],
        )

    if workplace != "remote":
        return EligibilityResult(False, "unknown", workplace, ["workplace type is unknown"])

    # Explicit residency language is more authoritative than a broad location.
    if windows:
        combined = " ".join(windows)
        if _contains_token(combined, GERMANY_TOKENS):
            return EligibilityResult(True, "germany", workplace, ["residency rule explicitly allows Germany"])
        if _contains_token(combined, BROAD_REGIONS):
            return EligibilityResult(True, "eu", workplace, ["residency rule allows Europe/EMEA/worldwide"])
        if _contains_token(combined, NON_GERMANY_COUNTRIES):
            return EligibilityResult(False, "restricted", workplace, ["residency rule is limited to another country"])

    structured, reason = _structured_remote_decision(job)
    if structured is True:
        scope = "germany" if "germany" in reason.lower() else "eu"
        return EligibilityResult(True, scope, workplace, [reason or "structured eligibility permits Germany"])
    if structured is False:
        return EligibilityResult(False, "restricted", workplace, [reason or "structured eligibility excludes Germany"])

    if _contains_token(loc, GERMANY_TOKENS):
        return EligibilityResult(True, "germany", workplace, ["remote location includes Germany"])
    if _contains_token(loc, BROAD_REGIONS):
        scope = "worldwide" if _contains_token(loc, {"worldwide", "global", "anywhere"}) else "eu"
        return EligibilityResult(True, scope, workplace, ["remote location covers Germany"])
    if _contains_token(loc, NON_GERMANY_COUNTRIES) or _contains_token(loc, NON_GERMANY_CODES):
        return EligibilityResult(False, "restricted", workplace, ["remote location is limited to another country"])

    return EligibilityResult(False, "unknown", workplace, ["remote eligibility for Germany is not stated"])


def apply_eligibility(job: Job) -> bool:
    result = evaluate_eligibility(job)
    job.workplace_type = result.workplace_type  # type: ignore[assignment]
    job.remote_scope = result.scope
    job.eligibility_status = "eligible" if result.eligible else "ineligible"
    job.eligibility_reasons = result.reasons
    logger.debug(
        "Eligibility {}: {} ({})", "ACCEPT" if result.eligible else "REJECT",
        job.title, "; ".join(result.reasons),
    )
    return result.eligible


def classify_remote_scope(job: Job) -> str:
    """Compatibility wrapper for older source/tests."""
    return evaluate_eligibility(job).scope


def passes_location_filter(job: Job) -> bool:
    """Compatibility wrapper used by the main filter pipeline."""
    return apply_eligibility(job)


COUNTRY_BLOCKLIST = sorted(NON_GERMANY_COUNTRIES)
