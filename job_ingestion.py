"""Reusable normal-job filtering, deduplication, and persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Sequence

import config
from filters.pipeline import run_filter_pipeline
from models.job import Job
from models.scan import FilterRejection, FilterRunSummary
from runtime_leases import job_ingestion_lease
from storage.database import (
    filter_unseen,
    find_existing_job_id,
    init_db,
    save_jobs,
)


class JobIngestionStatus(str, Enum):
    """Bounded terminal status for one discovered input."""

    ACCEPTED = "accepted"
    SAVED = "saved"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class JobIngestionCandidate:
    input_key: str
    job: Job


@dataclass(frozen=True, slots=True)
class JobIngestionItemResult:
    input_key: str
    status: JobIngestionStatus
    job_id: str | None = None
    rejection_code: str | None = None
    explanation: str = ""


@dataclass(slots=True)
class JobIngestionBatchResult:
    filter_summary: FilterRunSummary
    unseen_jobs: list[Job] = field(default_factory=list)
    saved_jobs: list[Job] = field(default_factory=list)
    item_results: tuple[JobIngestionItemResult, ...] = ()

    @property
    def accepted_jobs(self) -> list[Job]:
        return self.filter_summary.accepted_jobs


TerminalCallback = Callable[
    [tuple[JobIngestionItemResult, ...]], Awaitable[None]
]
FilterUnseen = Callable[[list[Job]], Awaitable[list[Job]]]
SaveJobs = Callable[[list[Job]], Awaitable[list[Job]]]
Pipeline = Callable[..., FilterRunSummary]


async def process_discovered_jobs(
    inputs: Sequence[Job] | Sequence[JobIngestionCandidate],
    *,
    persist: bool,
    max_age_days: int | None = None,
    verbose: bool = False,
    associate_items: bool = False,
    on_terminal: TerminalCallback | None = None,
    filter_unseen_fn: FilterUnseen = filter_unseen,
    save_jobs_fn: SaveJobs = save_jobs,
    pipeline_fn: Pipeline = run_filter_pipeline,
) -> JobIngestionBatchResult:
    """Run the authoritative normal Job pipeline for source or mail inputs.

    Fetching, source health, and delivery stay with their callers.  Only the
    dedup/save/terminal-association database section is leased.
    """

    candidates: list[JobIngestionCandidate] = []
    for index, value in enumerate(inputs):
        if isinstance(value, JobIngestionCandidate):
            candidates.append(value)
            associate_items = True
        else:
            candidates.append(JobIngestionCandidate(str(index), value))

    jobs = [candidate.job for candidate in candidates]
    summary = pipeline_fn(
        jobs,
        max_age_days=max_age_days,
        verbose=verbose or associate_items,
        settings=config,
    )
    batch = JobIngestionBatchResult(filter_summary=summary)

    results_by_object: dict[int, JobIngestionItemResult] = {}
    if associate_items:
        candidate_by_object = {id(candidate.job): candidate for candidate in candidates}
        for job, rejection in summary.verbose_rejections:
            candidate = candidate_by_object[id(job)]
            results_by_object[id(job)] = _rejected_result(candidate, rejection)

    if not persist:
        if associate_items:
            for job in summary.accepted_jobs:
                candidate = next(
                    item for item in candidates if item.job is job
                )
                results_by_object[id(job)] = JobIngestionItemResult(
                    input_key=candidate.input_key,
                    status=JobIngestionStatus.ACCEPTED,
                    job_id=job.id,
                )
            batch.item_results = _ordered_results(candidates, results_by_object)
        return batch

    await init_db()
    async with job_ingestion_lease():
        unseen = await filter_unseen_fn(summary.accepted_jobs)
        saved = await save_jobs_fn(unseen) if unseen else []
        batch.unseen_jobs = unseen
        batch.saved_jobs = saved

        if associate_items:
            unseen_objects = {id(job) for job in unseen}
            saved_objects = {id(job) for job in saved}
            candidate_by_object = {
                id(candidate.job): candidate for candidate in candidates
            }
            for job in summary.accepted_jobs:
                candidate = candidate_by_object[id(job)]
                if id(job) in saved_objects:
                    result = JobIngestionItemResult(
                        input_key=candidate.input_key,
                        status=JobIngestionStatus.SAVED,
                        job_id=job.id,
                    )
                else:
                    existing_id = await find_existing_job_id(job)
                    result = JobIngestionItemResult(
                        input_key=candidate.input_key,
                        status=JobIngestionStatus.DUPLICATE,
                        job_id=existing_id or (job.id if id(job) in unseen_objects else None),
                    )
                results_by_object[id(job)] = result
            batch.item_results = _ordered_results(candidates, results_by_object)

        if on_terminal is not None:
            await on_terminal(batch.item_results)

    return batch


def _rejected_result(
    candidate: JobIngestionCandidate,
    rejection: FilterRejection,
) -> JobIngestionItemResult:
    return JobIngestionItemResult(
        input_key=candidate.input_key,
        status=JobIngestionStatus.REJECTED,
        rejection_code=rejection.code.value,
        explanation=rejection.explanation[:160],
    )


def _ordered_results(
    candidates: Sequence[JobIngestionCandidate],
    results_by_object: dict[int, JobIngestionItemResult],
) -> tuple[JobIngestionItemResult, ...]:
    return tuple(
        results_by_object[id(candidate.job)]
        for candidate in candidates
        if id(candidate.job) in results_by_object
    )
