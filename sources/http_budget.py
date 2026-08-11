"""Scan-local concurrency budget for source-side HTTP attempts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class SourceHttpBudget:
    """Cancellation-safe semaphore with bounded runtime observations."""

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("source HTTP limit must be a positive integer")
        self.configured_limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self.current_usage = 0
        self.observed_peak = 0
        self.total_attempts = 0
        self.retry_count = 0
        self.rate_limit_count = 0

    def record_retry(self) -> None:
        self.retry_count += 1

    def record_rate_limit(self) -> None:
        self.rate_limit_count += 1

    @asynccontextmanager
    async def attempt(self) -> AsyncIterator[None]:
        """Acquire for exactly one network attempt and always release."""

        await self._semaphore.acquire()
        self.total_attempts += 1
        self.current_usage += 1
        self.observed_peak = max(self.observed_peak, self.current_usage)
        try:
            yield
        finally:
            self.current_usage -= 1
            self._semaphore.release()
