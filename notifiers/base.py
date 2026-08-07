"""Abstract base notifier — all notification channels implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, TypeAlias

from models.job import Job


DeliveryKind: TypeAlias = Literal["immediate", "digest", "explore"]
DeliveryDestination: TypeAlias = Literal[
    "discord_general",
    "discord_ngo",
    "telegram",
]

DELIVERY_KINDS = frozenset({"immediate", "digest", "explore"})
DELIVERY_DESTINATIONS = frozenset(
    {"discord_general", "discord_ngo", "telegram"}
)
DISCORD_DESTINATIONS = frozenset({"discord_general", "discord_ngo"})


@dataclass(frozen=True, slots=True)
class DeliverySuccess:
    """One job accepted by one logical notification destination."""

    job_id: str
    destination: DeliveryDestination


@dataclass(frozen=True, slots=True)
class GroupedJobPayload:
    """A bounded grouped payload and the exact jobs rendered into it."""

    title: str
    description: str
    jobs: tuple[Job, ...]


def resolve_discord_destination(
    job: Job,
    *,
    general_configured: bool,
    ngo_configured: bool,
) -> DeliveryDestination | None:
    """Resolve the one Discord obligation for a job deterministically."""

    if job.is_ngo and ngo_configured:
        return "discord_ngo"
    if general_configured:
        return "discord_general"
    return None


class BaseNotifier(ABC):
    """Base class for notification channels (Discord, Telegram, etc.)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this notifier (e.g. 'discord')."""

    @abstractmethod
    async def send_jobs(self, jobs: list[Job]) -> list[DeliverySuccess]:
        """Send a batch of job notifications.

        Implementations should handle rate-limiting and failures gracefully
        (log errors but don't crash the scan cycle) and return exact successes.
        """

    @abstractmethod
    async def send_test_message(self) -> None:
        """Send a test/health-check message to verify configuration."""
