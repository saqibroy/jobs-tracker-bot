"""Receipt-driven notification orchestration for bounded Phase 4A delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from filters.employment import employment_display_lines
from models.job import Job
from notifiers.base import (
    DeliveryDestination,
    DeliveryKind,
    DeliverySuccess,
    GroupedJobPayload,
    resolve_discord_destination,
)
from notifiers.discord_notifier import DiscordNotifier, _SOURCE_ICONS
from notifiers.telegram_notifier import TelegramNotifier
from storage.database import get_pending_delivery_jobs, record_delivery_receipts

_DELIVERY_BATCH_SIZE = 15
_PENDING_MAX_AGE_DAYS = 14
_DISCORD_DESCRIPTION_LIMIT = 3_900


@dataclass(frozen=True, slots=True)
class DeliveryRunResult:
    """Bounded summary of one coordinator run."""

    selected_count: int = 0
    included_count: int = 0
    successes: tuple[DeliverySuccess, ...] = ()


def _bounded(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _digest_line(job: Job) -> str:
    source_icon = _SOURCE_ICONS.get(job.source, "🌐")
    employment_text = "".join(
        f"\n> {_bounded(line, 180)}" for line in employment_display_lines(job)
    )
    return (
        f"{source_icon} **[{_bounded(job.title, 180)}]({_bounded(job.url, 500)})**\n"
        f"> 🏢 {_bounded(job.company, 160)}  ·  `{_bounded(job.source, 60)}`  ·  "
        f"📊 {job.match_score}%  ·  🧭 {_bounded(job.workplace_type, 30)}"
        f"{employment_text}"
    )


def build_digest_payload(
    jobs: list[Job],
    *,
    max_description_chars: int = _DISCORD_DESCRIPTION_LIMIT,
) -> GroupedJobPayload:
    """Render a bounded digest and retain its exact included membership."""

    if max_description_chars < 1:
        raise ValueError("max_description_chars must be positive")
    lines: list[str] = []
    included: list[Job] = []
    for job in jobs[:_DELIVERY_BATCH_SIZE]:
        line = _digest_line(job)
        candidate = "\n\n".join([*lines, line])
        if len(candidate) > max_description_chars:
            break
        lines.append(line)
        included.append(job)
    return GroupedJobPayload(
        title=f"📋 Digest — {len(included)} pending job{'s' if len(included) != 1 else ''}",
        description="\n\n".join(lines),
        jobs=tuple(included),
    )


async def _attempt_and_record(
    delivery_kind: DeliveryKind,
    destination: DeliveryDestination,
    jobs: list[Job],
    sender: Callable[[list[Job]], Awaitable[list[DeliverySuccess]]],
) -> list[DeliverySuccess]:
    # Provider HTTP and SQLite cannot share a transaction. A crash after an
    # accepted external send but before this receipt commit can therefore cause
    # one at-least-once duplicate on retry; normal retries remain destination-
    # idempotent once the receipt is durable.
    if not jobs:
        return []
    try:
        successes = list(await sender(jobs) or [])
    except Exception:
        logger.exception(
            "{} delivery batch failed for {}; leaving {} job(s) pending",
            delivery_kind,
            destination,
            len(jobs),
        )
        return []
    await record_delivery_receipts(delivery_kind, successes)
    return successes


async def process_pending_immediate_deliveries(
    *,
    discord_notifier: DiscordNotifier | None = None,
    telegram_notifier: TelegramNotifier | None = None,
) -> DeliveryRunResult:
    """Retry every configured immediate destination from durable pending state."""

    discord = discord_notifier or DiscordNotifier()
    telegram = telegram_notifier or TelegramNotifier()
    selected = 0
    successes: list[DeliverySuccess] = []

    for destination in ("discord_general", "discord_ngo"):
        if not discord.has_destination(destination):
            continue
        jobs = await get_pending_delivery_jobs(
            "immediate",
            destination,
            limit=_DELIVERY_BATCH_SIZE,
            max_age_days=_PENDING_MAX_AGE_DAYS,
            ngo_webhook_configured=discord.ngo_configured,
        )
        jobs = [
            job
            for job in jobs
            if resolve_discord_destination(
                job,
                general_configured=discord.general_configured,
                ngo_configured=discord.ngo_configured,
            )
            == destination
        ]
        selected += len(jobs)
        successes.extend(
            await _attempt_and_record(
                "immediate",
                destination,
                jobs,
                lambda batch: discord.send_jobs(
                    batch, include_batch_header=False
                ),
            )
        )

    if telegram.configured:
        jobs = await get_pending_delivery_jobs(
            "immediate",
            "telegram",
            limit=_DELIVERY_BATCH_SIZE,
            max_age_days=_PENDING_MAX_AGE_DAYS,
            ngo_webhook_configured=discord.ngo_configured,
        )
        selected += len(jobs)
        successes.extend(
            await _attempt_and_record(
                "immediate",
                "telegram",
                jobs,
                telegram.send_jobs,
            )
        )

    logger.info(
        "Immediate delivery processed {} pending obligation(s): {} succeeded",
        selected,
        len(successes),
    )
    return DeliveryRunResult(selected, selected, tuple(successes))


async def process_pending_digest_delivery(
    *,
    total_jobs: int,
    discord_notifier: DiscordNotifier | None = None,
) -> DeliveryRunResult:
    """Deliver one Discord-general digest batch and receipt exact membership."""

    discord = discord_notifier or DiscordNotifier()
    if not discord.general_configured:
        return DeliveryRunResult()

    jobs = await get_pending_delivery_jobs(
        "digest",
        "discord_general",
        limit=_DELIVERY_BATCH_SIZE,
        max_age_days=_PENDING_MAX_AGE_DAYS,
        ngo_webhook_configured=discord.ngo_configured,
    )
    payload = build_digest_payload(jobs)
    if not payload.jobs:
        return DeliveryRunResult(selected_count=len(jobs))

    try:
        successes = await discord.send_grouped_digest(payload, total_jobs=total_jobs)
    except Exception:
        logger.exception(
            "Digest delivery failed; leaving {} included job(s) pending",
            len(payload.jobs),
        )
        successes = []
    await record_delivery_receipts("digest", list(successes))
    logger.info(
        "Digest selected {}, included {}, delivered {} job(s)",
        len(jobs),
        len(payload.jobs),
        len(successes),
    )
    return DeliveryRunResult(
        selected_count=len(jobs),
        included_count=len(payload.jobs),
        successes=tuple(successes),
    )
