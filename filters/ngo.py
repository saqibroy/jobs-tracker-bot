"""NGO / nonprofit classifier.

Score-based: if score >= MIN_NGO_SCORE (default 1), flag job.is_ngo = True.

Scoring:
  +2  company name contains NGO-related terms
  +1  description contains social-impact keywords (requires 2+ distinct matches
      AND at least 1 company keyword match)
  +1  company is in the known-NGO list

Penalties:
  -2  NOT-NGO signals (marketplace, saas, fintech, etc.)
  -3  Strong NOT-NGO signals (assessment platform, hiring platform, etc.)
"""

from __future__ import annotations

from loguru import logger

import config
from models.job import Job

# ── Company-name signals (+2 each) ────────────────────────────────────────
_COMPANY_KEYWORDS: list[str] = [
    "foundation",
    "ngo",
    "nonprofit",
    "non-profit",
    "not-for-profit",
    "civil society",
    "charity",
    "humanitarian",
    "international development",
    "development organisation",
    "development organization",
]

# ── Description signals (+1 each match) ───────────────────────────────────
_DESCRIPTION_KEYWORDS: list[str] = [
    "social impact",
    "mission-driven",
    "mission driven",
    "501(c)",
    "civil rights",
    "human rights",
    "open data",
    "digital rights",
    "transparency",
    "advocacy",
    "policy",
    "public interest",
    "social good",
    "civic tech",
    "open source",
    "open society",
    "press freedom",
    "internet freedom",
]

# ── Known NGO names (case-insensitive match on company) ────────────────────
_KNOWN_NGOS: list[str] = [
    "tactical tech",
    "amnesty international",
    "amnesty",
    "oxfam",
    "msf",
    "médecins sans frontières",
    "unhcr",
    "greenpeace",
    "eff",
    "electronic frontier foundation",
    "open knowledge",
    "open knowledge foundation",
    "wikimedia",
    "wikimedia foundation",
    "mozilla foundation",
    "mozilla",
    "access now",
    "article 19",
    "privacy international",
    "reporters without borders",
    "transparency international",
    "human rights watch",
    "the engine room",
    "internews",
    "global witness",
    "center for democracy & technology",
    "free press",
    "fight for the future",
    "open rights group",
    "european digital rights",
    "edri",
    "witness",
    "signal foundation",
    "tor project",
    "freedom of the press foundation",
    "committee to protect journalists",
    "cpj",
    "ford foundation",
    "omidyar network",
    "luminate",
    "digital freedom fund",
]

# ── NOT-NGO signals (-2 each) ─────────────────────────────────────────────
# If the company name or description contains these, it's likely a
# for-profit company that happens to use mission-sounding language.
_NOT_NGO_SIGNALS: list[str] = [
    "marketplace",
    "e-commerce",
    "ecommerce",
    "saas",
    "b2b platform",
    "b2c platform",
    "fintech",
    "adtech",
    "ad tech",
    "proptech",
    "insurtech",
    "home services",
    "real estate platform",
    "venture-backed",
    "series a",
    "series b",
    "series c",
    "ipo",
]

# ── Strong NOT-NGO signals (-3 each) ──────────────────────────────────────
# These indicate clearly for-profit HR/SaaS companies that are definitely
# not NGOs, even if they use mission-sounding language in their postings.
_NOT_NGO_STRONG: list[str] = [
    "assessment platform",
    "hiring platform",
    "recruiting platform",
    "hr software",
    "hrtech",
    "hr tech",
    "talent assessment",
    "skills testing",
    "skills assessment",
    "saas platform",
    "b2b saas",
    "series a funded",
    "series b funded",
    "venture backed",
]


def compute_ngo_score(job: Job) -> int:
    """Return a numeric NGO score for the job (higher = more likely NGO)."""
    score = 0
    company_lower = job.company.lower()
    desc_lower = (job.description or "").lower()

    # Company name keywords (+2)
    company_kw_matches = 0
    for kw in _COMPANY_KEYWORDS:
        if kw in company_lower:
            score += 2
            company_kw_matches += 1
            break  # one match is enough for company

    # Known NGO list (+1)
    known_match = False
    for ngo in _KNOWN_NGOS:
        if ngo in company_lower:
            score += 1
            known_match = True
            break

    # Description keywords — require at least 2 distinct matches to score.
    # AND require at least 1 company keyword match — description alone is NOT
    # enough to classify as NGO (too many false positives).
    desc_matches = sum(1 for kw in _DESCRIPTION_KEYWORDS if kw in desc_lower)
    desc_score = 0
    if desc_matches >= 2 and (company_kw_matches >= 1 or known_match):
        desc_score = 1
        score += desc_score

    # NOT-NGO signals (-2 each match, check company + description)
    combined_lower = f"{company_lower} {desc_lower}"
    penalty = 0
    for signal in _NOT_NGO_SIGNALS:
        if signal in combined_lower:
            penalty += 2
            logger.debug("NGO penalty (-2 for '{}'): {}", signal, job.company)
            break  # one penalty is enough to override weak positives

    # Strong NOT-NGO signals (-3 each)
    for signal in _NOT_NGO_STRONG:
        if signal in combined_lower:
            penalty += 3
            logger.debug("NGO strong penalty (-3 for '{}'): {}", signal, job.company)
            break  # one strong penalty is enough

    score -= penalty

    logger.debug(
        "[ngo] {}: company_kw={}, desc_kw={}, known_list={}, "
        "penalties={}, total={} → is_ngo={}",
        job.company, company_kw_matches, desc_matches,
        known_match, penalty, max(score, 0),
        max(score, 0) >= config.MIN_NGO_SCORE,
    )

    return max(score, 0)  # floor at 0


def classify_ngo(job: Job) -> Job:
    """Set job.is_ngo based on the NGO score. Returns the (mutated) job."""
    score = compute_ngo_score(job)
    job.is_ngo = score >= config.MIN_NGO_SCORE
    if job.is_ngo:
        logger.debug("NGO MATCH (score={}): {} @ {}", score, job.title, job.company)
    return job
