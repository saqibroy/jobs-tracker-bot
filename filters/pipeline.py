"""Global filter orchestration with terminal rejection accounting."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

import config as default_config
from filters.employment import classify_employment, employment_rejection_reason
from filters.language import (
    evaluate_language,
    language_rejection_explanation,
    passes_language_filter,
)
from filters.location import passes_location_filter
from filters.match import compute_match_score
from filters.notification_policy import (
    apply_freelance_permission_ceiling,
    assign_notification_tier,
    select_company_candidates,
)
from filters.ngo import classify_ngo
from filters.role import passes_role_profile_filter as passes_role_filter
from filters.stack import passes_stack_filter
from filters.profile import (
    EmploymentPolicy,
    LanguagePolicy,
    NotificationPolicy,
    load_employment_policy,
    load_language_policy,
    load_notification_policy,
)
from models.job import Job
from models.scan import (
    FilterRejection,
    FilterRunSummary,
    RejectionCode,
    SourceFunnelMetrics,
    SourceStatus,
    empty_rejection_counts,
    utc_now,
)

SENIOR_ACCEPT = {"senior", "lead", "staff", "principal", "head", "director", "architect"}
SENIOR_REJECT = {"junior", "mid-level", "mid level", "entry-level", "entry level"}
SALARY_NUM_RE = re.compile(r"[\d,.]+")


def passes_company_blocklist(job: Job, settings: Any = default_config) -> bool:
    """Return whether a job's company is absent from the configured blocklist."""

    if not settings.COMPANY_BLOCKLIST:
        return True
    company_lower = job.company.lower().strip()
    return not any(blocked in company_lower for blocked in settings.COMPANY_BLOCKLIST)


def passes_senior_filter(job: Job, settings: Any = default_config) -> bool:
    """Preserve the existing optional senior-only compatibility gate."""

    if not settings.FILTER_SENIOR_ONLY:
        return True
    title_lower = job.title.lower()
    if any(keyword in title_lower for keyword in SENIOR_ACCEPT):
        return True
    if any(keyword in title_lower for keyword in SENIOR_REJECT):
        return False
    return True


def passes_salary_filter(job: Job, settings: Any = default_config) -> bool:
    """Preserve the existing optional minimum-salary gate."""

    if settings.MIN_SALARY_EUR <= 0 or not job.salary:
        return True
    values = SALARY_NUM_RE.findall(job.salary.replace(",", ""))
    if not values:
        return True
    try:
        salary_value = float(values[0])
        if salary_value < 10000:
            salary_value *= 12
        return salary_value >= settings.MIN_SALARY_EUR
    except (ValueError, IndexError):
        return True


def _sort_key(job: Job) -> datetime:
    value = job.posted_at or job.fetched_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _new_source_metrics(source: str, now: datetime) -> SourceFunnelMetrics:
    return SourceFunnelMetrics(
        source=source,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        status=SourceStatus.HEALTHY,
    )


def run_filter_pipeline(
    jobs: list[Job],
    max_age_days: int | None = None,
    verbose: bool = False,
    *,
    settings: Any = default_config,
    max_jobs_per_company: int | None = None,
    employment_policy: EmploymentPolicy | None = None,
    language_policy: LanguagePolicy | None = None,
    notification_policy: NotificationPolicy | None = None,
    apply_company_cap: bool = True,
) -> FilterRunSummary:
    """Run the existing global filter order and count one terminal result/job."""

    accepted_before_cap: list[Job] = []
    verbose_rejections: list[tuple[Job, FilterRejection]] = []
    seen_content_hashes: set[str] = set()
    rejection_counts: Counter[RejectionCode] = Counter()
    per_source: dict[str, SourceFunnelMetrics] = {}
    now = utc_now()
    current_employment_policy = employment_policy or load_employment_policy()
    current_notification_policy = notification_policy or load_notification_policy()

    for job in jobs:
        source = job.source or "unknown"
        metrics = per_source.setdefault(source, _new_source_metrics(source, now))
        metrics.raw_count += 1

    def reject(job: Job, code: RejectionCode, explanation: str) -> None:
        rejection = FilterRejection(code=code, explanation=explanation)
        rejection_counts[code] += 1
        source = job.source or "unknown"
        metrics = per_source.setdefault(source, _new_source_metrics(source, now))
        metrics.rejection_counts[code] += 1
        if verbose:
            verbose_rejections.append((job, rejection))

    for job in sorted(jobs, key=_sort_key, reverse=True):
        if job.content_hash in seen_content_hashes:
            logger.debug("Dedup SKIP (in-memory): {}", job.title)
            reject(job, RejectionCode.DUPLICATE_IN_MEMORY, "dedup (content hash)")
            continue

        if not passes_company_blocklist(job, settings):
            logger.info(
                "[{}] Rejected: {} at {} (company blocklist)",
                job.source,
                job.title,
                job.company,
            )
            reject(
                job,
                RejectionCode.COMPANY_BLOCKLIST,
                f"company blocklist: '{job.company}'",
            )
            continue

        if not passes_location_filter(job):
            reject(
                job,
                RejectionCode.LOCATION,
                f"eligibility: {'; '.join(job.eligibility_reasons)}",
            )
            continue

        classify_employment(job)
        employment_reason = employment_rejection_reason(
            job, current_employment_policy
        )
        if employment_reason:
            reject(
                job,
                RejectionCode.EMPLOYMENT_RELATIONSHIP,
                f"employment: {employment_reason}",
            )
            continue

        if not passes_role_filter(job):
            reject(
                job,
                RejectionCode.ROLE,
                f"role: no dev keyword in title '{job.title}'",
            )
            continue

        if not passes_stack_filter(job):
            reject(
                job,
                RejectionCode.STACK,
                f"stack: incompatible stack in '{job.title}'",
            )
            continue

        language_passes = (
            evaluate_language(job, language_policy)
            if language_policy is not None
            else passes_language_filter(job)
        )
        if not language_passes:
            applied_language_policy = language_policy or load_language_policy()
            reject(
                job,
                RejectionCode.LANGUAGE,
                language_rejection_explanation(job, applied_language_policy),
            )
            continue

        if not passes_senior_filter(job, settings):
            reject(
                job,
                RejectionCode.SENIORITY,
                f"senior filter: title '{job.title}' has junior/mid-level",
            )
            continue

        if not passes_salary_filter(job, settings):
            reject(
                job,
                RejectionCode.SALARY,
                f"salary filter: '{job.salary}' below min {settings.MIN_SALARY_EUR}",
            )
            continue

        source_max = settings.SOURCE_MAX_AGE_DAYS.get(job.source)
        if source_max is not None:
            effective_max_age = source_max
        elif max_age_days is not None:
            effective_max_age = max_age_days
        else:
            effective_max_age = settings.MAX_JOB_AGE_DAYS
        if job.posted_at is not None:
            posted = job.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - posted
            if age > timedelta(days=effective_max_age):
                age_days = age.total_seconds() / 86400
                logger.debug(
                    "Recency REJECT ({:.0f}d old, max {}d): {}",
                    age_days,
                    effective_max_age,
                    job.title,
                )
                reject(
                    job,
                    RejectionCode.RECENCY,
                    f"recency: {age_days:.0f}d old (max {effective_max_age}d)",
                )
                continue

        classify_ngo(job)
        try:
            job.match_score = compute_match_score(job)
        except Exception as exc:
            logger.warning(
                "Match scoring failed for '{}': {} — defaulting to 0",
                job.title,
                exc,
            )
            job.match_score = 0

        score_tier = assign_notification_tier(
            job.match_score,
            current_notification_policy,
        )
        job.notification_tier = apply_freelance_permission_ceiling(
            job,
            score_tier,
            current_notification_policy,
        )

        if settings.MINIMUM_MATCH_SCORE > 0 and job.match_score < settings.MINIMUM_MATCH_SCORE:
            reject(
                job,
                RejectionCode.MINIMUM_SCORE,
                f"match score: {job.match_score}% < minimum {settings.MINIMUM_MATCH_SCORE}%",
            )
            continue

        # Preserve the existing behavior: only a job that reaches this point
        # claims its content hash, even if the later company cap rejects it.
        seen_content_hashes.add(job.content_hash)
        accepted_before_cap.append(job)

    if apply_company_cap:
        cap = (
            current_notification_policy.max_jobs_per_company
            if max_jobs_per_company is None
            else max_jobs_per_company
        )
        selection = select_company_candidates(
            accepted_before_cap,
            cap,
            mode="diversity",
        )
        accepted = list(selection.selected)
        for job in selection.excluded:
            reject(
                job,
                RejectionCode.COMPANY_CAP,
                f"company cap: {cap} higher-tier/diverse roles kept",
            )
    else:
        accepted = accepted_before_cap

    for job in accepted:
        per_source[job.source or "unknown"].accepted_count += 1

    summary = FilterRunSummary(
        accepted_jobs=accepted,
        raw_count=len(jobs),
        rejection_counts={code: rejection_counts.get(code, 0) for code in RejectionCode},
        per_source=per_source,
        verbose_rejections=verbose_rejections,
    )
    summary.validate_accounting()

    logger.info("Filters: {} in → {} accepted", len(jobs), len(accepted))
    logger.debug(
        "[match] {} jobs accepted, {} with match_score set",
        len(accepted),
        sum(1 for job in accepted if job.match_score is not None),
    )
    return summary


def rejection_pairs(summary: FilterRunSummary) -> list[tuple[Job, str]]:
    """Compatibility shape used by the existing CLI rejection printer."""

    return [(job, rejection.explanation) for job, rejection in summary.verbose_rejections]
