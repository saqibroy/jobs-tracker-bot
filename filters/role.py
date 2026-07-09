"""CV-profile role gate.

The gate removes jobs that cannot plausibly match before scoring. Scoring then
ranks the remaining full-stack/frontend and strongly aligned backend roles.
"""

from __future__ import annotations

import re

from loguru import logger

from filters.profile import profile_list
from models.job import Job


def _contains(text: str, values: list[str]) -> bool:
    for value in values:
        if len(value) <= 3:
            if re.search(rf"\b{re.escape(value)}\b", text):
                return True
        elif value in text:
            return True
    return False


def role_rejection_reason(job: Job) -> str | None:
    title = job.title.lower()
    full_text = f"{title} {' '.join(job.tags).lower()} {(job.description or '').lower()}"
    rejects = profile_list("roles", "reject")
    primary = profile_list("roles", "primary")
    secondary = profile_list("roles", "secondary")
    core_stack = profile_list("stack", "core")

    if _contains(title, rejects):
        return "title is outside the target role/seniority profile"
    seniority_text = f"{title} {' '.join(job.tags).lower()}"
    if _contains(
        seniority_text,
        ["intern", "internship", "working student", "werkstudent", "junior", "entry level", "entry-level"],
    ):
        return "job seniority is below the target profile"

    primary_match = _contains(title, primary)
    secondary_match = _contains(title, secondary)
    stack_match = _contains(full_text, core_stack)

    if primary_match:
        return None
    if secondary_match and stack_match:
        return None
    return "role is not full-stack/frontend or a strongly aligned backend/web role"


def passes_role_filter(job: Job) -> bool:
    reason = role_rejection_reason(job)
    if reason:
        logger.debug("Role REJECT ({}): {}", reason, job.title)
        return False
    return True
