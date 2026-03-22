"""Role / tech-stack keyword filter.

Two-stage filter:
  1. REJECT if title matches a non-dev role or intern/student pattern.
  2. ACCEPT if title or tags or description (first 200 chars) contain a dev keyword.

This ensures we only surface developer / engineering positions and
exclude non-dev roles even if they happen to contain a broad keyword
like "engineer".
"""

from __future__ import annotations

from loguru import logger

from models.job import Job

# ── Hard-reject patterns (checked on TITLE only) ──────────────────────────
# If any of these appear in the title, reject immediately — these are
# clearly non-dev roles that might slip through broad keywords.
_TITLE_REJECT_PATTERNS: list[str] = [
    # Non-dev roles
    "office assistant",
    "executive assistant",
    "virtual assistant",
    "brand manager",
    "marketing manager",
    "marketing director",
    "marketing lead",
    "growth marketing",
    "sales",
    "account executive",
    "account manager",
    "business development",
    "customer success",
    "customer support",
    "support agent",
    "recruiter",
    "recruiting",
    "talent acquisition",
    "human resources",
    " hr ",  # spaces to avoid matching "chrome", "share" etc.
    "hr manager",
    "hr director",
    "people ops",
    "people operations",
    "finance manager",
    "accountant",
    "bookkeeper",
    "content writer",
    "copywriter",
    "social media",
    "graphic designer",
    "ui designer",
    "ux designer",  # keep "ux engineer" via positive match
    "project manager",  # keep "technical project manager" via positive match
    "scrum master",
    "agile coach",
    # Sales/marketing hybrids
    "go to market",
    "go-to-market",
    "gtm engineer",
    # Product roles (not engineering)
    "product manager",
    "senior product manager",
    "staff product manager",
    "principal product manager",
    "head of product",
    # Web3/blockchain (not in user's stack)
    "smart contract",
    "blockchain engineer",
    "web3 engineer",
    "solidity",
    "defi engineer",
    "crypto engineer",
    "svm engineer",  # Solana Virtual Machine
    # Seniority/type we don't want
    "intern ",       # trailing space to avoid matching "internal" / "internet"
    "intern,",
    "intern-",
    "internship",
    "working student",
    "werkstudent",
    "praktikum",
    "praktikant",
    "apprentice",
    "trainee",
    "c-suite",
    "chief",
    "vp of",
    "vice president",
    "android engineer",
    "ios engineer",
    "mobile engineer",  # native mobile
    "embedded",
    "firmware",
    "hardware engineer",
    "data analyst",
    "business analyst",
    "financial analyst",
    "seo specialist",
    "seo manager",  # not seo engineer/developer
]

# ── Keywords that indicate a relevant tech role ────────────────────────────
# Checked on title + tags + description (first 200 chars).
_ROLE_KEYWORDS: list[str] = [
    # Core titles
    "full stack", "fullstack", "full-stack",
    "frontend", "front-end", "front end",
    "backend", "back-end", "back end",
    "software engineer", "software developer",
    "software development engineer",
    "web developer", "web engineer",
    "web application developer",
    "api developer", "api engineer",
    "platform developer", "platform engineer",
    "solutions engineer",
    "integration engineer",
    "technical lead",
    "staff engineer",
    "principal engineer",
    "site reliability engineer", "sre",
    "cloud engineer",
    "systems engineer",
    "application developer",
    "application engineer",

    # Languages & frameworks
    "react", "next.js", "nextjs", "vue", "vue.js", "nuxt",
    "typescript", "javascript", "node.js", "nodejs",
    "python", "django", "fastapi", "flask",
    "ruby on rails", "ruby", "rails", "php", "symfony", "laravel",

    # Infrastructure & tools
    "docker", "kubernetes", "ci/cd", "gitlab ci", "github actions",
    "devops", "site reliability",

    # Data & AI
    "postgresql", "mysql", "graphql", "apollo graphql", "rest api",
    "llm", "rag", "ai engineer", "machine learning engineer",

    # Other tech
    "tailwindcss", "tailwind",
    "accessibility", "a11y",
    "seo engineer", "seo developer",

    # Broad but useful — caught by title context
    "developer",
    "engineer",
]


def passes_role_filter(job: Job) -> bool:
    """Return True if the job title or tags or description match a target tech role
    AND the title does not match a reject pattern."""
    title_lower = job.title.lower()

    # ── Stage 1: hard reject on title ───────────────────────────────────
    for pattern in _TITLE_REJECT_PATTERNS:
        if pattern in title_lower:
            logger.debug("Role REJECT (title reject '{}'): {}", pattern, job.title)
            return False

    # Also catch titles that START with "Intern " or equal "Intern"
    title_stripped = title_lower.strip()
    if title_stripped == "intern" or title_stripped.startswith("intern "):
        logger.debug("Role REJECT (intern title): {}", job.title)
        return False

    # ── Stage 2: require positive dev keyword match ─────────────────────
    tags_lower = " ".join(job.tags).lower()
    desc_snippet = (job.description or "")[:200].lower()
    combined = f"{title_lower} {tags_lower} {desc_snippet}"

    for keyword in _ROLE_KEYWORDS:
        if keyword in combined:
            logger.debug("Role ACCEPT ('{}'): {}", keyword, job.title)
            return True

    logger.debug("Role REJECT (no dev keyword): {}", job.title)
    return False
