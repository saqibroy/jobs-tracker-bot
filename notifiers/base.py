"""Abstract base notifier — all notification channels implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.job import Job


class BaseNotifier(ABC):
    """Base class for notification channels (Discord, Telegram, etc.)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this notifier (e.g. 'discord')."""

    @abstractmethod
    async def send_jobs(self, jobs: list[Job]) -> None:
        """Send a batch of job notifications.

        Implementations should handle rate-limiting and failures gracefully
        (log errors but don't crash the scan cycle).
        """

    @abstractmethod
    async def send_test_message(self) -> None:
        """Send a test/health-check message to verify configuration."""
