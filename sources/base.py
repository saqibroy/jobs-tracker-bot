"""Abstract base class for all job sources.

Every source must implement `fetch()` which returns a list[Job].
The base class provides:
  - httpx AsyncClient with timeout + retry logic
  - rate-limit handling (429 → skip this run)
  - structured logging
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from loguru import logger

import config
from models.job import Job
from models.scan import (
    MAX_COMPONENT_ISSUE_DETAILS,
    SanitizedSourceIssue,
    SourceComponentError,
    SourceFetchOutcome,
    SourceStatus,
    classify_source_exception,
    dominant_failure_status,
    sanitize_component_identifier,
    sanitize_source_error,
    utc_now,
)
from sources.http_budget import SourceHttpBudget


class BaseSource(ABC):
    """Base class for job board integrations."""

    name: str = "base"  # override in subclass

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(config.HTTP_TIMEOUT)
        self._last_request_issue: SanitizedSourceIssue | None = None
        self._component_success_count = 0
        self._component_issue_count = 0
        self._component_issues: list[SanitizedSourceIssue] = []
        self._component_failure_statuses: set[SourceStatus] = set()
        self._source_http_budget: SourceHttpBudget | None = None

    def bind_http_budget(self, budget: SourceHttpBudget | None) -> None:
        """Bind this adapter to the budget owned by its current scan."""

        self._source_http_budget = budget

    @asynccontextmanager
    async def _http_attempt(self) -> AsyncIterator[None]:
        budget = self._source_http_budget
        if budget is None:
            yield
            return
        async with budget.attempt():
            yield

    def _remember_request_issue(
        self,
        error: BaseException | str,
        status: SourceStatus | None = None,
    ) -> None:
        """Remember one request issue for list-returning adapter compatibility."""

        self._last_request_issue = SanitizedSourceIssue.from_error(error, status)

    def _record_component_success(self) -> None:
        """Record one board, endpoint, page, or query that completed."""

        self._component_success_count += 1

    def _record_component_issue(
        self,
        component: object,
        error: BaseException | str,
        status: SourceStatus | None = None,
    ) -> None:
        """Count a failed component and retain at most five safe summaries."""

        resolved = status or (
            classify_source_exception(error)
            if isinstance(error, BaseException)
            else SourceStatus.UNKNOWN_ERROR
        )
        self._component_issue_count += 1
        self._component_failure_statuses.add(resolved)
        if len(self._component_issues) < MAX_COMPONENT_ISSUE_DETAILS:
            self._component_issues.append(
                SanitizedSourceIssue.from_error(error, resolved, component)
            )

    def _consume_component_results(
        self,
        items: list,
        results: list,
        component_identifier: Callable[[object], object],
    ) -> list[Job]:
        """Merge successful list results and record each failed sibling unit."""

        jobs: list[Job] = []
        for item, result in zip(items, results):
            if isinstance(result, Exception):
                identifier = component_identifier(item)
                self._record_component_issue(identifier, result)
                logger.warning(
                    "[{}] Component '{}' failed: {}",
                    self.name,
                    sanitize_component_identifier(identifier),
                    sanitize_source_error(result),
                )
                continue
            if not isinstance(result, list):
                self._record_component_issue(
                    component_identifier(item),
                    TypeError("component fetch result is not a list"),
                )
                continue
            self._record_component_success()
            jobs.extend(result)
        return jobs

    @staticmethod
    def _require_component_response(response: httpx.Response) -> httpx.Response:
        """Raise for the 429 response that the retry wrapper returns directly."""

        if int(getattr(response, "status_code", 200)) >= 400:
            response.raise_for_status()
        return response

    @staticmethod
    def _fail_component(status: SourceStatus, explanation: str) -> None:
        """Raise a bounded status-aware component failure without response data."""

        raise SourceComponentError(status, explanation)

    async def _map_bounded(self, items: list, worker) -> list:
        """Run one fan-out boundary with a fixed-size local worker pool."""

        if not items:
            return []
        results: list[object] = [None] * len(items)
        pending = iter(enumerate(items))
        worker_count = min(
            len(items),
            max(1, config.MAX_CONCURRENT_SOURCE_COMPONENTS),
        )

        async def run_worker() -> None:
            for index, item in pending:
                try:
                    results[index] = await worker(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    results[index] = exc

        tasks = [
            asyncio.create_task(run_worker(), name=f"{self.name}-component-worker")
            for _ in range(worker_count)
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results

    # ── Retry-enabled HTTP GET ──────────────────────────────────────────
    async def _get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """GET with retries + exponential backoff.

        Raises httpx.HTTPStatusError on non-retryable failures.
        """
        last_exc: Exception | None = None
        for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
            try:
                async with self._http_attempt():
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.get(url, params=params, headers=headers)

                    # Rate limited — skip this run entirely
                    if resp.status_code == 429:
                        if self._source_http_budget is not None:
                            self._source_http_budget.record_rate_limit()
                        self._remember_request_issue(
                            "HTTP 429 rate limited",
                            SourceStatus.RATE_LIMITED,
                        )
                        logger.warning(
                            "[{}] Rate limited (429) — skipping this cycle", self.name
                        )
                        return resp  # caller should check status

                    # Redirects usually mean a stale board slug or a provider
                    # migration. They are permanent for this scan and must not
                    # be followed into an unrelated marketing page.
                    if 300 <= resp.status_code < 400:
                        exc = httpx.HTTPStatusError(
                            f"unexpected redirect to {resp.headers.get('location', '')}",
                            request=resp.request,
                            response=resp,
                        )
                        self._remember_request_issue(exc)
                        raise exc
                    resp.raise_for_status()
                    return resp

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                self._remember_request_issue(exc)
                status = exc.response.status_code
                if status < 500 and status != 429:
                    logger.warning(
                        "[{}] Permanent HTTP {} for {} — not retrying",
                        self.name, status, sanitize_source_error(str(exc.request.url)),
                    )
                    raise
                if attempt >= config.HTTP_MAX_RETRIES:
                    break
                wait = min(2 ** attempt, 8)
                if self._source_http_budget is not None:
                    self._source_http_budget.record_retry()
                logger.warning(
                    "[{}] Attempt {}/{} failed: {} — retrying in {}s",
                    self.name,
                    attempt,
                    config.HTTP_MAX_RETRIES,
                    sanitize_source_error(exc),
                    wait,
                )
                await asyncio.sleep(wait)
            except httpx.RequestError as exc:
                last_exc = exc
                self._remember_request_issue(exc)
                if attempt >= config.HTTP_MAX_RETRIES:
                    break
                wait = min(2 ** attempt, 8)
                if self._source_http_budget is not None:
                    self._source_http_budget.record_retry()
                logger.warning(
                    "[{}] Attempt {}/{} failed: {} — retrying in {}s",
                    self.name,
                    attempt,
                    config.HTTP_MAX_RETRIES,
                    sanitize_source_error(exc),
                    wait,
                )
                await asyncio.sleep(wait)

        # All retries exhausted
        logger.error("[{}] All {} retries failed", self.name, config.HTTP_MAX_RETRIES)
        raise last_exc  # type: ignore[misc]

    # ── Retry-enabled HTTP POST ─────────────────────────────────────────
    async def _post(
        self,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """POST with retries + exponential backoff (for JSON APIs like Algolia)."""
        last_exc: Exception | None = None
        for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
            try:
                async with self._http_attempt():
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.post(url, json=json_body, headers=headers, params=params)

                    if resp.status_code == 429:
                        if self._source_http_budget is not None:
                            self._source_http_budget.record_rate_limit()
                        self._remember_request_issue(
                            "HTTP 429 rate limited",
                            SourceStatus.RATE_LIMITED,
                        )
                        logger.warning(
                            "[{}] Rate limited (429) — skipping this cycle", self.name
                        )
                        return resp

                    if 300 <= resp.status_code < 400:
                        exc = httpx.HTTPStatusError(
                            f"unexpected redirect to {resp.headers.get('location', '')}",
                            request=resp.request,
                            response=resp,
                        )
                        self._remember_request_issue(exc)
                        raise exc
                    resp.raise_for_status()
                    return resp

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                self._remember_request_issue(exc)
                status = exc.response.status_code
                if status < 500 and status != 429:
                    logger.warning(
                        "[{}] Permanent HTTP {} for {} — not retrying",
                        self.name, status, sanitize_source_error(str(exc.request.url)),
                    )
                    raise
                if attempt >= config.HTTP_MAX_RETRIES:
                    break
                wait = min(2 ** attempt, 8)
                if self._source_http_budget is not None:
                    self._source_http_budget.record_retry()
                logger.warning(
                    "[{}] POST attempt {}/{} failed: {} — retrying in {}s",
                    self.name,
                    attempt,
                    config.HTTP_MAX_RETRIES,
                    sanitize_source_error(exc),
                    wait,
                )
                await asyncio.sleep(wait)
            except httpx.RequestError as exc:
                last_exc = exc
                self._remember_request_issue(exc)
                if attempt >= config.HTTP_MAX_RETRIES:
                    break
                wait = min(2 ** attempt, 8)
                if self._source_http_budget is not None:
                    self._source_http_budget.record_retry()
                logger.warning(
                    "[{}] POST attempt {}/{} failed: {} — retrying in {}s",
                    self.name,
                    attempt,
                    config.HTTP_MAX_RETRIES,
                    sanitize_source_error(exc),
                    wait,
                )
                await asyncio.sleep(wait)

        logger.error("[{}] All {} POST retries failed", self.name, config.HTTP_MAX_RETRIES)
        raise last_exc  # type: ignore[misc]

    # ── Abstract interface ──────────────────────────────────────────────
    @abstractmethod
    async def fetch(self) -> list[Job]:
        """Fetch job listings and return normalized Job objects.

        Implementations should:
          - Call self._get() to make HTTP requests
          - Parse the response
          - Return a list of Job objects (no filtering — that's done later)
          - Handle malformed data gracefully (skip bad entries, don't crash)
        """
        ...

    async def safe_fetch(self) -> list[Job]:
        """Wrapper around fetch() that catches all exceptions so one
        broken source never crashes the whole scan cycle."""
        return (await self.fetch_outcome()).jobs

    async def fetch_outcome(self) -> SourceFetchOutcome:
        """Fetch jobs and preserve whether an empty list was healthy or failed."""

        self._last_request_issue = None
        self._component_success_count = 0
        self._component_issue_count = 0
        self._component_issues = []
        self._component_failure_statuses = set()
        started_at = utc_now()
        started_clock = time.perf_counter()
        try:
            jobs = await self.fetch()
            if not isinstance(jobs, list):
                raise TypeError("source fetch result is not a list")

            issue = self._last_request_issue
            if self._component_issue_count:
                issues = tuple(self._component_issues)
                if self._component_success_count or jobs:
                    status = SourceStatus.PARTIAL_SUCCESS
                else:
                    status = dominant_failure_status(
                        SanitizedSourceIssue(item, item.value)
                        for item in self._component_failure_statuses
                    )
            elif self._component_success_count:
                status = SourceStatus.HEALTHY if jobs else SourceStatus.ZERO_RESULTS
                issues = ()
            elif jobs:
                status = SourceStatus.HEALTHY
                issues = ()
            elif issue is not None:
                status = issue.status
                issues = (issue,)
            else:
                status = SourceStatus.ZERO_RESULTS
                issues = ()
            logger.info(
                "[{}] Fetched {} raw jobs ({}, {} component issues)",
                self.name,
                len(jobs),
                status.value,
                self._component_issue_count,
            )
        except Exception as exc:
            status = classify_source_exception(exc)
            issue = SanitizedSourceIssue.from_error(exc, status)
            issues = (issue,)
            jobs = []
            logger.error("[{}] Fetch failed ({}): {}", self.name, status.value, issue.explanation)

        completed_at = utc_now()
        return SourceFetchOutcome(
            source=self.name,
            jobs=jobs,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, round((time.perf_counter() - started_clock) * 1000)),
            issues=issues,
            component_issue_count=self._component_issue_count,
        )
