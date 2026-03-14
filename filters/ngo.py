"""NGO / nonprofit classifier.

Score-based: if score >= MIN_NGO_SCORE (default 1), flag job.is_ngo = True.

Scoring:
  +2  company name contains NGO-related terms
  +1  description contains social-impact keywords
  +1  company is in the known-NGO list
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


def compute_ngo_score(job: Job) -> int:
    """Return a numeric NGO score for the job (higher = more likely NGO)."""
    score = 0
    company_lower = job.company.lower()
    desc_lower = (job.description or "").lower()

    # Company name keywords (+2)
    for kw in _COMPANY_KEYWORDS:
        if kw in company_lower:
            score += 2
            break  # one match is enough for company

    # Known NGO list (+1)
    for ngo in _KNOWN_NGOS:
        if ngo in company_lower:
            score += 1
            break

    # Description keywords — require at least 2 distinct matches to score.
    # A single word like "transparency" or "policy" appears in generic corporate
    # postings too often. Two or more signals are a much stronger indicator.
    desc_matches = sum(1 for kw in _DESCRIPTION_KEYWORDS if kw in desc_lower)
    if desc_matches >= 2:
        score += 1

    # NOT-NGO signals (-2 each match, check company + description)
    combined_lower = f"{company_lower} {desc_lower}"
    for signal in _NOT_NGO_SIGNALS:
        if signal in combined_lower:
            score -= 2
            logger.debug("NGO penalty (-2 for '{}'): {}", signal, job.company)
            break  # one penalty is enough to override weak positives

    return max(score, 0)  # floor at 0


def classify_ngo(job: Job) -> Job:
    """Set job.is_ngo based on the NGO score. Returns the (mutated) job."""
    score = compute_ngo_score(job)
    job.is_ngo = score >= config.MIN_NGO_SCORE
    if job.is_ngo:
        logger.debug("NGO MATCH (score={}): {} @ {}", score, job.title, job.company)
    return job
