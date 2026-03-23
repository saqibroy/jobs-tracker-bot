"""Stack compatibility filter.

Rejects jobs whose title/tags mention ONLY incompatible tech stacks
with zero mention of the user's actual stack.

If a job title says "Java Spring Boot Developer" and there's no mention
of React, Python, TypeScript, etc. anywhere in the title or tags,
it's a pure mismatch → reject.

If the title is ambiguous (e.g., "Software Engineer") or mentions both
Java and React, we let it through — other filters will score it.
"""

from __future__ import annotations

from loguru import logger

from models.job import Job

# ── Incompatible stack signals ─────────────────────────────────────────────
# If ANY of these appear in title/tags AND NONE of the user's stack
# appears → REJECT.
INCOMPATIBLE_STACKS: list[str] = [
    # JVM ecosystem
    "java", "spring", "kotlin",
    # Microsoft / systems
    "c++", "c#", ".net", "dotnet",
    # Other systems languages
    "golang",
    "rust",
    # Legacy / enterprise platforms
    "cobol", "fortran", "mainframe",
    "salesforce", "sap",
    # Native mobile
    "ios developer", "swift developer",
    "android developer", "kotlin developer",
    # Game engines
    "unity developer", "unreal engine",
    # Microsoft ERP
    "c/al", "navision", "dynamics",
]

# ── User's stack signals ──────────────────────────────────────────────────
# If any of these appear alongside incompatible stacks, the job is
# ambiguous → keep it (e.g., "Full Stack Java + React" is still relevant).
USER_STACK_SIGNALS: list[str] = [
    "react", "next", "vue", "nuxt", "typescript", "javascript",
    "python", "django", "fastapi", "rails", "ruby",
    "php", "symfony", "laravel", "node", "graphql",
    "tailwind", "frontend", "full stack", "fullstack", "full-stack",
    "web developer", "web engineer",
]


def passes_stack_filter(job: Job) -> bool:
    """Return True if the job is NOT a pure incompatible-stack role.

    Logic:
    - If no incompatible stack mentioned → ACCEPT (ambiguous/generic title)
    - If incompatible stack mentioned AND user stack also mentioned → ACCEPT
    - If incompatible stack mentioned AND NO user stack mentioned → REJECT
    """
    title_lower = job.title.lower()
    tags_lower = " ".join(job.tags).lower() if job.tags else ""
    combined = f"{title_lower} {tags_lower}"

    # Special word-boundary handling for "go" to avoid matching "good", "google"
    has_incompatible = False
    for signal in INCOMPATIBLE_STACKS:
        if signal in combined:
            has_incompatible = True
            break

    # Also check for standalone "go" with word boundary
    if not has_incompatible:
        import re
        if re.search(r"\bgo\b", combined):
            # Only count if it looks like Go-the-language context
            go_context_words = ["golang", "go developer", "go engineer", "go backend"]
            if any(w in combined for w in go_context_words):
                has_incompatible = True

    if not has_incompatible:
        return True  # no incompatible stack → accept (generic title)

    has_user_stack = any(signal in combined for signal in USER_STACK_SIGNALS)

    if has_user_stack:
        logger.debug(
            "Stack ACCEPT (mixed stacks): {}",
            job.title,
        )
        return True  # both stacks mentioned → ambiguous, keep

    logger.debug(
        "Stack REJECT (incompatible only): {}",
        job.title,
    )
    return False  # pure incompatible stack, reject
