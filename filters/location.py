"""Location / remote-eligibility filter.

Accept a job if it matches ANY of:
  1. Fully remote, open worldwide (with corroboration)
  2. Remote within EU (excluding UK) — includes per-country EU matching
  3. Remote/hybrid in Germany / Berlin
  4. "Must be located in [EU country]" + remote work

Reject:
  - UK-only remote
  - Pure on-site (including Berlin on-site with no remote/hybrid mention)
  - US-only or other non-EU remote restrictions
  - scope=unknown (default to reject — we don't know where it's accessible)
  - Country blocklist matches (unless overridden by EU/worldwide)
"""

from __future__ import annotations

from loguru import logger

import config
from models.job import Job

# ── Remote-only boards ─────────────────────────────────────────────────────
# These boards only list remote positions, so "Worldwide" is trustworthy.
_REMOTE_ONLY_BOARDS: set[str] = {"remoteok", "weworkremotely", "remotive"}

# ── Full EU/EEA + DACH/Benelux country list (lowercase) ───────────────────
# Used to detect "Remote - Spain", "work from Portugal", etc.
_EU_COUNTRIES: list[str] = [
    # DACH
    "germany", "deutschland", "austria", "österreich", "switzerland", "schweiz",
    # Western Europe
    "france", "spain", "portugal", "italy", "netherlands", "belgium",
    "luxembourg", "ireland",
    # Nordics
    "sweden", "denmark", "norway", "finland", "iceland",
    # Central/Eastern Europe
    "poland", "czech republic", "czechia", "romania", "hungary",
    "slovakia", "slovenia", "croatia", "bulgaria",
    # Baltics
    "estonia", "latvia", "lithuania",
    # Southern/other
    "greece", "cyprus", "malta",
    # Regional groupings people use in job posts
    "dach", "benelux",
]

# German cities — for Germany-specific matching
_GERMAN_CITIES: list[str] = [
    "berlin", "münchen", "munich", "hamburg", "frankfurt", "köln",
    "cologne", "düsseldorf", "stuttgart", "leipzig", "dresden",
    "hannover", "nuremberg", "nürnberg", "dortmund", "essen", "bremen",
]

# ── Blocklist patterns (lowercase) ─────────────────────────────────────────
_BLOCK_PATTERNS: list[str] = [
    "uk only",
    "united kingdom only",
    "gb only",
    "london only",
    "us only",
    "usa only",
    "u.s. only",
    "united states only",
    "canada only",
]

# Things that signal the UK is the sole location
_UK_SIGNALS: list[str] = [
    "uk only",
    "united kingdom only",
    "gb only",
    "london only",
    "uk-based",
]

# ── Country blocklist ──────────────────────────────────────────────────────
# If the location field contains any of these as the primary location, REJECT.
# Exception: if location ALSO contains "worldwide" or "europe" or an EU
# country name → override blocklist and ACCEPT.
COUNTRY_BLOCKLIST: list[str] = [
    "united states", "usa", "us only", "remote us", "remote - us",
    "canada", "canada only", "remote canada",
    "australia", "new zealand", "brazil", "india", "nigeria",
    "singapore", "japan", "south korea", "china",
    "united kingdom", "uk only", "london", "england",
    "mexico", "argentina", "colombia",
]

# ── Remote/hybrid signal keywords ──────────────────────────────────────────
_REMOTE_KEYWORDS: list[str] = [
    "remote",
    "work from anywhere",
    "work from home",
    "home office",
    "distributed",
    "telecommute",
    "hybrid",
    "work from",  # "work from France", "work from home", etc.
]

_WORLDWIDE_KEYWORDS: list[str] = [
    "worldwide",
    "global",
    "anywhere",
    "work from anywhere",
]

_EU_REGION_KEYWORDS: list[str] = [
    "europe",
    "european union",
    "eu ",      # trailing space to avoid matching "neural"
    "eu,",      # "EU, UK"
    "eu/",      # "EU/UK"
    "(eu)",
    "emea",
    "remote - eu",
    "remote - europe",
    "remote eu",
    "remote europe",
]

# ── "Must reside in" patterns ─────────────────────────────────────────────
_RESIDENCY_PHRASES: list[str] = [
    "must be located in",
    "must reside in",
    "must be based in",
    "based in",
    "resident in",
    "residing in",
    "eligible to work in",
    "located in",
    "living in",
    "work from",
]

# ── On-site signals ───────────────────────────────────────────────────────
_ONSITE_SIGNALS: list[str] = [
    "on-site",
    "on site",
    "onsite",
    "in-office",
    "in office",
    "office-based",
]

# ── Non-EU country / region patterns (location field only) ─────────────────
# If the *location* string mentions one of these AND doesn't also mention an
# EU keyword or "worldwide", the job is region-locked outside the EU.
_NON_EU_LOCATION_PATTERNS: list[str] = [
    "united states", "united kingdom",
    # Country-code style
    "us", "usa", "u.s.",
    "uk", "u.k.",
    "canada", "australia", "india", "brazil", "mexico",
    "singapore", "japan", "china", "south korea", "israel",
    "south africa", "new zealand", "philippines", "nigeria",
    "argentina", "colombia",
    # US states / regions
    "california", "new york", "texas", "florida", "illinois",
    "massachusetts", "washington", "colorado", "georgia", "virginia",
    "north carolina", "pennsylvania", "ohio", "michigan", "arizona",
    "oregon", "minnesota", "tennessee", "maryland", "connecticut",
    # US / Canadian / non-EU cities
    "san francisco", "los angeles", "new york city", "nyc",
    "seattle", "austin", "boston", "chicago", "denver",
    "atlanta", "miami", "portland", "dallas", "houston",
    "san diego", "philadelphia", "phoenix", "detroit", "charlotte",
    "nashville", "raleigh", "salt lake city", "minneapolis",
    "tampa", "pittsburgh", "st. louis", "baltimore",
    "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "london", "manchester", "edinburgh", "bristol", "leeds",
    "sydney", "melbourne", "brisbane",
    "mumbai", "bangalore", "hyderabad", "delhi",
    # Region patterns in job location fields
    "remote - us", "remote us", "remote, us", "remote (us)",
    "remote - usa", "remote usa", "remote, usa",
    "remote - united states", "remote, united states",
    "remote - canada", "remote, canada", "remote (canada)",
    "remote - uk", "remote, uk", "remote (uk)",
    "americas",
    "apac",
    "latam",
    "latin america",
]


def _lower(text: str | None) -> str:
    return (text or "").lower()


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def _mentions_eu_country(text: str) -> bool:
    """Return True if the text mentions any EU/EEA country."""
    return any(country in text for country in _EU_COUNTRIES)


def _mentions_germany(text: str) -> bool:
    """Return True if the text mentions Germany or a German city."""
    if "germany" in text or "deutschland" in text:
        return True
    return any(city in text for city in _GERMAN_CITIES)


def _mentions_non_eu_location(loc: str) -> bool:
    """Return True if the *location field* contains a non-EU pattern.

    Uses word-boundary-aware matching for short tokens (us, uk, usa, etc.)
    to avoid false positives like 'focus' matching 'us'.
    """
    import re

    # Short tokens that need word-boundary matching
    _SHORT_TOKENS = {"us", "usa", "u.s.", "uk", "u.k."}

    for pattern in _NON_EU_LOCATION_PATTERNS:
        if pattern in _SHORT_TOKENS:
            # Word-boundary match to avoid 'focus' → 'us', 'duck' → 'uk'
            if re.search(rf"\b{re.escape(pattern)}\b", loc):
                return True
        else:
            if pattern in loc:
                return True
    return False


def _has_remote_signal(text: str) -> bool:
    """Return True if the text contains any remote/hybrid work signal."""
    return _has_any(text, _REMOTE_KEYWORDS)


def _has_residency_with_eu_country(text: str) -> bool:
    """Return True if text has a 'must be located in [EU country]' pattern.

    We look for a residency phrase followed (within ~80 chars) by an EU country.
    """
    for phrase in _RESIDENCY_PHRASES:
        idx = text.find(phrase)
        if idx != -1:
            # Check the ~80 chars after the phrase for an EU country
            window = text[idx:idx + len(phrase) + 80]
            if _mentions_eu_country(window):
                return True
    return False


def _matches_country_blocklist(loc: str) -> bool:
    """Return True if the location matches a country blocklist entry.

    Uses word-boundary matching for short tokens to avoid false positives.
    """
    import re

    _SHORT_BLOCKLIST_TOKENS = {"usa", "london", "england"}

    for entry in COUNTRY_BLOCKLIST:
        if entry in _SHORT_BLOCKLIST_TOKENS or len(entry) <= 4:
            if re.search(rf"\b{re.escape(entry)}\b", loc):
                return True
        else:
            if entry in loc:
                return True
    return False


def _has_worldwide_override(loc: str) -> bool:
    """Return True if the location also mentions worldwide/europe/EU country,
    which overrides a country blocklist match."""
    if _has_any(loc, _WORLDWIDE_KEYWORDS):
        return True
    if _has_any(loc, _EU_REGION_KEYWORDS):
        return True
    if _mentions_eu_country(loc):
        return True
    return False


def classify_remote_scope(job: Job) -> str:
    """Determine the remote scope from location + tags.

    Priority: Germany > EU country > EU region > Worldwide (corroborated) >
              Country blocklist > Non-EU pattern > generic remote.
    """
    loc = _lower(job.location)
    tags_text = _lower(" ".join(job.tags))
    loc_and_tags = f"{loc} {tags_text}"
    desc_snippet = _lower((job.description or "")[:500])

    # 1. Germany — highest specificity
    if _mentions_germany(loc_and_tags):
        return "germany"

    # 2. Specific EU country in location/tags
    if _mentions_eu_country(loc_and_tags):
        return "eu"

    # 3. EU region keywords (location + tags + description)
    combined = f"{loc_and_tags} {desc_snippet}"
    if _has_any(combined, _EU_REGION_KEYWORDS):
        return "eu"

    # 4. Residency requirement in EU country (description)
    if _has_residency_with_eu_country(combined):
        return "eu"

    # 5. Worldwide — only trust if corroborated:
    #    - Source is a remote-only board (remoteok, weworkremotely, remotive)
    #    - Location explicitly says "Worldwide", "Work from anywhere", etc.
    #    For arbeitnow: "Worldwide" without other signal → treat as germany.
    if _has_any(loc_and_tags, _WORLDWIDE_KEYWORDS):
        # Check if the source is trustworthy for worldwide claims
        if job.source in _REMOTE_ONLY_BOARDS:
            return "worldwide"
        # Explicit worldwide in location field is trustworthy for most sources
        if _has_any(loc, ["worldwide", "work from anywhere", "remote - worldwide", "global"]):
            # But for arbeitnow, just "Worldwide" alone is suspicious
            if job.source == "arbeitnow":
                # Only trust if there are OTHER signals too (e.g., description)
                if _has_any(desc_snippet, _WORLDWIDE_KEYWORDS):
                    return "worldwide"
                # Default arbeitnow "Worldwide" → germany
                return "germany"
            return "worldwide"
        return "worldwide"

    # 6. Country blocklist check — if location matches a blocked country
    #    and does NOT have an EU/worldwide override, mark as restricted.
    if _matches_country_blocklist(loc):
        if not _has_worldwide_override(loc):
            return "restricted"

    # 7. Non-EU country/city in location field → restricted
    #    Only check the *location* string (not tags/description) to avoid
    #    false positives from tag lists like "python, aws, us team ok".
    if _mentions_non_eu_location(loc):
        return "restricted"

    # 8. Generic remote — now returns "unknown" which will be REJECTED
    #    by the location filter (unknown = we don't know = don't notify)
    if _has_remote_signal(combined):
        return "unknown"

    return "unknown"


def passes_location_filter(job: Job) -> bool:
    """Return True if the job's location/remote status meets our criteria.

    Key rule: scope=unknown is REJECTED by default. Only accept if scope
    is explicitly worldwide, eu, or germany.
    """
    loc = _lower(job.location)
    tags_text = _lower(" ".join(job.tags))
    desc_snippet = _lower((job.description or "")[:500])
    combined = f"{loc} {tags_text} {desc_snippet}"

    # ── Pre-classified remote jobs ──────────────────────────────────────
    # If the caller (main.py) already set a valid remote_scope AND the job
    # is flagged remote by the API, trust that classification.
    if job.remote_scope in ("worldwide", "eu", "germany") and job.is_remote:
        logger.debug("Location ACCEPT (pre-classified {} + is_remote): {}", job.remote_scope, job.title)
        return True

    # ── Restricted scope: country/region-locked, not EU ─────────────────
    if job.remote_scope == "restricted":
        logger.debug("Location REJECT (restricted scope): {}", job.title)
        return False

    # ── Unknown scope: DEFAULT TO REJECT ────────────────────────────────
    # Unknown scope = we don't know where it's accessible from = don't notify.
    if job.remote_scope == "unknown":
        logger.debug("Location REJECT (unknown scope — default reject): {}", job.title)
        return False

    # ── Hard reject: blocklisted locations ──────────────────────────────
    for pattern in _BLOCK_PATTERNS:
        if pattern in combined:
            # But don't reject if it ALSO says worldwide
            if _has_any(combined, _WORLDWIDE_KEYWORDS):
                continue
            logger.debug("Location REJECT (blocklist '{}'): {}", pattern, job.title)
            return False

    # ── UK-only check ───────────────────────────────────────────────────
    if _has_any(combined, _UK_SIGNALS):
        logger.debug("Location REJECT (UK signal): {}", job.title)
        return False

    # ── Country blocklist check ─────────────────────────────────────────
    if _matches_country_blocklist(loc):
        if not _has_worldwide_override(loc):
            logger.debug("Location REJECT (country blocklist): {}", job.title)
            return False

    # ── Accept: worldwide remote ────────────────────────────────────────
    if _has_any(combined, _WORLDWIDE_KEYWORDS):
        logger.debug("Location ACCEPT (worldwide): {}", job.title)
        return True

    # ── Accept: EU region keywords ──────────────────────────────────────
    if _has_any(combined, _EU_REGION_KEYWORDS):
        logger.debug("Location ACCEPT (EU region): {}", job.title)
        return True

    # ── Germany/Berlin: require remote or hybrid signal ─────────────────
    # Check full description (not just snippet) because many German job
    # boards mention "remote" or "home office" deep in the posting.
    if _mentions_germany(combined):
        full_desc = _lower(job.description or "")
        full_text = f"{loc} {tags_text} {full_desc}"
        if _has_remote_signal(full_text):
            logger.debug("Location ACCEPT (Germany + remote/hybrid): {}", job.title)
            return True
        # On-site or no signal — reject
        logger.debug("Location REJECT (Germany, no remote/hybrid signal): {}", job.title)
        return False

    # ── Accept: specific EU country + remote signal ─────────────────────
    if _mentions_eu_country(combined) and _has_remote_signal(combined):
        logger.debug("Location ACCEPT (EU country + remote): {}", job.title)
        return True

    # ── Accept: EU country explicitly in location field ─────────────────
    if _mentions_eu_country(loc):
        logger.debug("Location ACCEPT (EU country in location): {}", job.title)
        return True

    # ── Accept: residency requirement in EU country ─────────────────────
    if _has_residency_with_eu_country(combined):
        logger.debug("Location ACCEPT (residency in EU country): {}", job.title)
        return True

    # ── No known eligible scope — reject ────────────────────────────────
    logger.debug("Location REJECT (no eligible scope): {}", job.title)
    return False
