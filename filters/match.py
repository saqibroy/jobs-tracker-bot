"""Match score calculator.

Computes a 0–100% match score for a job based on how well it matches
the user's specific tech stack. Higher score = better match.

Score = sum of matched keyword weights, normalized:
  - Raw score > 60 → 95%+ match
  - Linear scale below that

Negative weights penalize mismatched stacks (Java, C++, etc.).
"""

from __future__ import annotations

import re

from loguru import logger

from models.job import Job

# ── User's tech stack — weighted by preference ─────────────────────────────
STACK_WEIGHTS: dict[str, int] = {
    # TIER 1 — Core daily stack (15-20 points each)
    "react": 20,
    "next.js": 18, "nextjs": 18,
    "typescript": 16,
    "vue": 15, "vue.js": 15,
    "nuxt": 14, "nuxt.js": 14,
    "fastapi": 16,
    "django": 15,
    "tailwind": 12, "tailwindcss": 12,
    "python": 14,

    # TIER 2 — Strong secondary skills (8-12 points)
    "node": 12, "node.js": 12, "nodejs": 12,
    "ruby on rails": 12, "rails": 10, "ruby": 8,
    "graphql": 10,
    "postgresql": 8, "postgres": 8,
    "langchain": 12,
    "rag": 12,
    "llm": 10,
    "javascript": 10,

    # TIER 3 — Supporting skills (4-8 points)
    "php": 7, "symfony": 8,
    "docker": 6,
    "gitlab": 5, "ci/cd": 5,
    "rest api": 5,
    "mysql": 5,
    "mongodb": 4,
    "jest": 4, "vitest": 4, "rspec": 4,
    "wcag": 5, "accessibility": 5,
    "stripe": 4,
    "redis": 3,

    # TIER 4 — NGO/mission signals (bonus, not tech)
    "ngo": 15, "nonprofit": 15, "non-profit": 15,
    "social impact": 10, "mission-driven": 10,
    "open source": 6, "digital rights": 8,
    "civic tech": 8, "humanitarian": 8,
    "tactical tech": 20,  # direct match to previous employer type

    # NEGATIVE weights (penalize mismatched stack)
    "java": -5,
    "spring boot": -5,
    "c++": -8,
    "c#": -5,
    ".net": -5,
    "kubernetes": -3,
    "ansible": -3,
    "terraform": -3,
}


def _normalize_score(raw: int) -> int:
    """Normalize raw score to 0–100.

    Calibrated for recalibrated weights (v1.5):
      raw > 40 → 90-100%
      raw 25-40 → 70-89%
      raw 15-25 → 50-69%
      raw 5-15  → 20-49%
      raw < 5   → 0-19%
    """
    if raw <= 0:
        return 0
    if raw >= 50:
        return min(95 + int((raw - 50) * 5 / 30), 100)
    if raw >= 40:
        # 40..50 → 90..95
        return 90 + int((raw - 40) * 5 / 10)
    if raw >= 25:
        # 25..40 → 70..90
        return 70 + int((raw - 25) * 20 / 15)
    if raw >= 15:
        # 15..25 → 50..70
        return 50 + int((raw - 15) * 20 / 10)
    if raw >= 5:
        # 5..15 → 20..50
        return 20 + int((raw - 5) * 30 / 10)
    # 0..5 → 0..20
    return int(raw * 20 / 5)


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
    _SHORT_KEYWORDS = {"ai", "vue", "php", "rag", "llm", "c#", "c++"}

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
        "nodejs": "nodejs", "node": "nodejs", "node.js": "nodejs",
        "tailwind": "tailwind", "tailwindcss": "tailwind",
        "ngo": "ngo", "nonprofit": "ngo", "non-profit": "ngo",
        "vue": "vue", "vue.js": "vue",
        "nuxt": "nuxt", "nuxt.js": "nuxt",
        "postgresql": "postgresql", "postgres": "postgresql",
        "ruby on rails": "rails", "rails": "rails",
    }
    return _GROUPS.get(keyword, keyword)


def match_score_bar(score: int) -> str:
    """Render a 10-block Unicode bar for the match score.

    Example: 78% → ████████░░
    """
    filled = round(score / 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty
