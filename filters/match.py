"""Match score calculator.

Computes a 0–100% match score for a job based on how well it matches
the user's specific tech stack. Higher score = better match.

Score = sum of matched keyword weights, normalized:
  - Raw score > 60 → 95%+ match
  - Linear scale below that
"""

from __future__ import annotations

import re

from loguru import logger

from models.job import Job

# ── User's tech stack — weighted by preference ─────────────────────────────
STACK_WEIGHTS: dict[str, int] = {
    # High value (the core stack)
    "react": 15, "typescript": 12, "nextjs": 12, "next.js": 12,
    "python": 10, "django": 10, "fastapi": 8,
    "postgresql": 8, "graphql": 8, "rest api": 6,
    "node": 8, "nodejs": 8,

    # Medium value
    "docker": 6, "ci/cd": 5, "github actions": 5, "gitlab ci": 5,
    "tailwind": 4, "tailwindcss": 4,
    "vue": 4, "javascript": 5,
    "llm": 7, "rag": 7, "ai": 5,

    # Bonus signals
    "ngo": 10, "nonprofit": 10, "social impact": 8,
    "open source": 5, "digital rights": 6, "civic tech": 8,

    # Stack mentions in description
    "ruby": 4, "rails": 4, "php": 3, "symfony": 3,
    "mysql": 3, "redis": 3,
}


def _normalize_score(raw: int) -> int:
    """Normalize raw score to 0–100.

    If raw > 60 → 95+, scaling linearly below that.
    """
    if raw <= 0:
        return 0
    if raw >= 60:
        # Scale 60..100 → 95..100
        return min(95 + int((raw - 60) * 5 / 40), 100)
    # Linear scale: 0..60 → 0..95
    return int(raw * 95 / 60)


def compute_match_score(job: Job) -> int:
    """Compute a match score (0–100) for the job.

    Checks title, tags, and first 500 chars of description against
    the weighted keyword map.
    """
    title_lower = job.title.lower()
    tags_lower = " ".join(job.tags).lower()
    desc_lower = (job.description or "")[:500].lower()
    combined = f"{title_lower} {tags_lower} {desc_lower}"

    # Also check company for NGO-related keywords
    company_lower = job.company.lower()
    full_text = f"{combined} {company_lower}"

    raw_score = 0
    matched: list[str] = []

    # Track already-counted keywords to avoid double-counting synonyms
    counted_groups: set[str] = set()

    # Short keywords that need word-boundary matching
    _SHORT_KEYWORDS = {"ai", "vue", "php", "rag", "llm"}

    for keyword, weight in STACK_WEIGHTS.items():
        if keyword in _SHORT_KEYWORDS:
            if not re.search(rf"\b{re.escape(keyword)}\b", full_text):
                continue
        elif keyword not in full_text:
            continue

        # Avoid double-counting synonym groups
        group = _get_synonym_group(keyword)
        if group in counted_groups:
            continue
        counted_groups.add(group)
        raw_score += weight
        matched.append(keyword)

    # NGO bonus: if the job is flagged as NGO, add bonus
    if job.is_ngo and "ngo" not in counted_groups and "nonprofit" not in counted_groups:
        raw_score += 10
        matched.append("ngo (flag)")

    score = _normalize_score(raw_score)

    if matched:
        logger.debug(
            "Match score {}: {} (raw={}, keywords={})",
            score, job.title, raw_score, ", ".join(matched[:8]),
        )

    return score


def _get_synonym_group(keyword: str) -> str:
    """Map synonymous keywords to a canonical group name."""
    _GROUPS = {
        "nextjs": "nextjs", "next.js": "nextjs",
        "nodejs": "nodejs", "node": "nodejs",
        "tailwind": "tailwind", "tailwindcss": "tailwind",
        "ngo": "ngo", "nonprofit": "ngo",
    }
    return _GROUPS.get(keyword, keyword)


def match_score_bar(score: int) -> str:
    """Render a 10-block Unicode bar for the match score.

    Example: 78% → ████████░░
    """
    filled = round(score / 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty
