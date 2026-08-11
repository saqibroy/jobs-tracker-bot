"""One-process coordination for complete production source scan lifecycles."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator


@dataclass(frozen=True, slots=True)
class ActiveScan:
    scope: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class ScanBusyResult:
    """Bounded manual-command response that exposes no source/job details."""

    active_scope: str
    active_started_at: datetime

    @property
    def message(self) -> str:
        return (
            "Scan already in progress "
            f"(scope={self.active_scope}, started={self.active_started_at.isoformat()})"
        )


class ProductionScanCoordinator:
    """Serialize scheduled scans fairly and reject overlapping manual scans."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: ActiveScan | None = None

    @property
    def active(self) -> ActiveScan | None:
        return self._active

    def busy_result(self) -> ScanBusyResult | None:
        active = self._active
        if active is None:
            return None
        return ScanBusyResult(active.scope, active.started_at)

    @asynccontextmanager
    async def scheduled(self, scope: str) -> AsyncIterator[ActiveScan]:
        """Wait in ``asyncio.Lock`` FIFO order for a scheduled scan permit."""

        await self._lock.acquire()
        active = ActiveScan(scope=scope, started_at=datetime.now(timezone.utc))
        self._active = active
        try:
            yield active
        finally:
            self._active = None
            self._lock.release()

    @asynccontextmanager
    async def manual(self, scope: str) -> AsyncIterator[ActiveScan | ScanBusyResult]:
        """Acquire immediately when idle; never queue behind an active scan."""

        busy = self.busy_result()
        if busy is not None or self._lock.locked():
            if busy is None:
                # A scheduled waiter can own the lock before publishing state
                # only within the same event-loop tick; expose bounded fallback.
                now = datetime.now(timezone.utc)
                busy = ScanBusyResult("production", now)
            yield busy
            return

        await self._lock.acquire()
        active = ActiveScan(scope=scope, started_at=datetime.now(timezone.utc))
        self._active = active
        try:
            yield active
        finally:
            self._active = None
            self._lock.release()
