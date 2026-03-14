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

                    resp.raise_for_status()
                    return resp

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                wait = 2 ** attempt  # 2, 4, 8 seconds
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
