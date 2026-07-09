"""Explainable CV-fit scoring for jobs that already passed hard eligibility."""

from __future__ import annotations

import re

from filters.profile import profile_list, profile_value
from models.job import Job


def _normalize_score(raw: int) -> int:
    """Legacy helper retained for callers that score an already weighted sum."""
    if raw <= 0:
        return 0
    if raw >= 50:
        return min(95 + int((raw - 50) * 5 / 30), 100)
    if raw >= 40:
        return 90 + int((raw - 40) * 5 / 10)
    if raw >= 25:
        return 70 + int((raw - 25) * 20 / 15)
    if raw >= 15:
        return 50 + int((raw - 15) * 20 / 10)
    if raw >= 5:
        return 20 + int((raw - 5) * 30 / 10)
    return int(raw * 20 / 5)


def _matched(text: str, values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if len(value) <= 3:
            present = bool(re.search(rf"\b{re.escape(value)}\b", text))
        else:
            present = value in text
        if present:
            found.append(value)
    return found


def compute_match_score(job: Job) -> int:
    title = job.title.lower()
    text = f"{title} {' '.join(job.tags).lower()} {(job.description or '').lower()}"

    primary_roles = _matched(title, profile_list("roles", "primary"))
    secondary_roles = _matched(title, profile_list("roles", "secondary"))
    core = _matched(text, profile_list("stack", "core"))
    supporting = _matched(text, profile_list("stack", "supporting"))
    incompatible = _matched(text, profile_list("stack", "incompatible"))
    mission = _matched(text + " " + job.company.lower(), profile_list("mission", "keywords"))

    if primary_roles:
        role_score = 40
    elif secondary_roles:
        role_score = 26
    else:
        role_score = 0

    # Core skills matter much more than generic tooling. Synonyms are capped so
    # repeated framework spellings cannot inflate the score beyond the dimension.
    core_groups = {
        value.replace(".js", "").replace("js", "").replace(" ", "").replace("-", "")
        for value in core
    }
    stack_score = min(35, len(core_groups) * 8 + min(len(supporting), 3) * 3)
    if incompatible and not core:
        stack_score = 0
    elif incompatible:
        stack_score = max(0, stack_score - min(10, len(incompatible) * 3))
    if _matched(title, profile_list("stack", "incompatible")):
        # A mismatched backend language in the title is central to the role,
        # not an incidental technology mentioned later in the description.
        stack_score = min(stack_score, 10)

    if any(value in title for value in ("senior", "staff", "lead", "principal")):
        seniority_score = 10
    elif any(value in title for value in ("mid-level", "mid level", "professional")):
        seniority_score = 8
    else:
        seniority_score = 6

    mission_score = min(10, len(mission) * 5 + (5 if job.is_ngo else 0))
    work_model_score = 5 if job.workplace_type in ("remote", "hybrid", "onsite") else 0

    breakdown = {
        "role": role_score,
        "stack": stack_score,
        "seniority": seniority_score,
        "mission": mission_score,
        "work_model": work_model_score,
    }
    score = min(100, sum(breakdown.values()))
    reasons: list[str] = []
    if primary_roles or secondary_roles:
        reasons.append(f"role: {(primary_roles or secondary_roles)[0]}")
    if core:
        reasons.append("core stack: " + ", ".join(core[:5]))
    if supporting:
        reasons.append("supporting: " + ", ".join(supporting[:3]))
    if mission:
        reasons.append("mission: " + ", ".join(mission[:2]))
    if incompatible:
        reasons.append("mixed stack: " + ", ".join(incompatible[:3]))

    job.match_breakdown = breakdown
    job.match_reasons = reasons
    immediate = int(profile_value("notifications", "immediate_score", 70))
    digest = int(profile_value("notifications", "digest_score", 45))
    job.notification_tier = "immediate" if score >= immediate else "digest" if score >= digest else "none"
    return score


def match_score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)
