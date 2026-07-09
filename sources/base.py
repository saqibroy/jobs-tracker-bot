"""Abstract base class for all job sources.

Every source must implement `fetch()` which returns a list[Job].
The base class provides:
  - httpx AsyncClient with timeout + retry logic
  - rate-limit handling (429 → skip this run)
  - structured logging
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import httpx
from loguru import logger

import config
from models.job import Job


class BaseSource(ABC):
    """Base class for job board integrations."""

    name: str = "base"  # override in subclass

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(config.HTTP_TIMEOUT)

    async def _map_bounded(self, items: list, worker) -> list:
        """Run per-board requests with the configured concurrency ceiling."""
        semaphore = asyncio.Semaphore(max(1, config.MAX_CONCURRENT_SOURCES))

        async def run(item):
            async with semaphore:
                return await worker(item)

        return await asyncio.gather(
            *(run(item) for item in items),
            return_exceptions=True,
        )

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
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url, params=params, headers=headers)

                    # Rate limited — skip this run entirely
                    if resp.status_code == 429:
                        logger.warning(
                            "[{}] Rate limited (429) — skipping this cycle", self.name
                        )
                        return resp  # caller should check status

                    # Redirects usually mean a stale board slug or a provider
                    # migration. They are permanent for this scan and must not
                    # be followed into an unrelated marketing page.
                    if 300 <= resp.status_code < 400:
                        raise httpx.HTTPStatusError(
                            f"unexpected redirect to {resp.headers.get('location', '')}",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()
                    return resp

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status < 500 and status != 429:
                    logger.warning(
                        "[{}] Permanent HTTP {} for {} — not retrying",
                        self.name, status, exc.request.url,
                    )
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning(
                    "[{}] Attempt {}/{} failed: {} — retrying in {}s",
                    self.name, attempt, config.HTTP_MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)
            except httpx.RequestError as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.warning(
                    "[{}] Attempt {}/{} failed: {} — retrying in {}s",
                    self.name,
                    attempt,
                    config.HTTP_MAX_RETRIES,
                    exc,
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
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=json_body, headers=headers, params=params)

                    if resp.status_code == 429:
                        logger.warning(
                            "[{}] Rate limited (429) — skipping this cycle", self.name
                        )
                        return resp

                    if 300 <= resp.status_code < 400:
                        raise httpx.HTTPStatusError(
                            f"unexpected redirect to {resp.headers.get('location', '')}",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()
                    return resp

            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status < 500 and status != 429:
                    logger.warning(
                        "[{}] Permanent HTTP {} for {} — not retrying",
                        self.name, status, exc.request.url,
                    )
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning(
                    "[{}] POST attempt {}/{} failed: {} — retrying in {}s",
                    self.name, attempt, config.HTTP_MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)
            except httpx.RequestError as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.warning(
                    "[{}] POST attempt {}/{} failed: {} — retrying in {}s",
                    self.name, attempt, config.HTTP_MAX_RETRIES, exc, wait,
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
        try:
            jobs = await self.fetch()
            logger.info("[{}] Fetched {} raw jobs", self.name, len(jobs))
            return jobs
        except Exception as exc:
            logger.error("[{}] Fetch failed: {}", self.name, exc)
            return []
