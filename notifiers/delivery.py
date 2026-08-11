"""Receipt-driven notification orchestration for bounded policy delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from filters.employment import employment_display_lines
from filters.notification_policy import employment_bucket
from filters.profile import NotificationPolicy, load_notification_policy
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
from runtime_leases import immediate_delivery_lease
from storage.database import get_pending_delivery_jobs, record_delivery_receipts

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
    max_items: int | None = None,
) -> GroupedJobPayload:
    """Render a bounded employment-grouped digest with exact membership."""

    policy = load_notification_policy()
    return _build_grouped_payload(
        jobs,
        delivery_kind="digest",
        max_items=policy.digest_max_items if max_items is None else max_items,
        max_description_chars=max_description_chars,
    )


def build_explore_payload(
    jobs: list[Job],
    *,
    max_description_chars: int = _DISCORD_DESCRIPTION_LIMIT,
    max_items: int | None = None,
) -> GroupedJobPayload:
    """Render a bounded employment-grouped explore digest."""

    policy = load_notification_policy()
    return _build_grouped_payload(
        jobs,
        delivery_kind="explore",
        max_items=policy.explore_max_items if max_items is None else max_items,
        max_description_chars=max_description_chars,
    )


def _build_grouped_payload(
    jobs: list[Job],
    *,
    delivery_kind: DeliveryKind,
    max_items: int,
    max_description_chars: int,
) -> GroupedJobPayload:
    """Render every included job once in mutually exclusive employment sections."""

    if max_description_chars < 1:
        raise ValueError("max_description_chars must be positive")
    if max_items < 1 or max_items > 25:
        raise ValueError("max_items must be an integer from 1 to 25")
    sections = (
        ("standard", "**Standard / strong employee roles**"),
        ("part_contract", "**Part-time / contract / fixed-term**"),
        ("freelance", "**Freelance**"),
    )
    grouped: dict[str, list[Job]] = {name: [] for name, _ in sections}
    for job in jobs:
        bucket = employment_bucket(job)
        section = (
            "freelance"
            if bucket == "freelance"
            else "part_contract"
            if bucket in {"part_time", "contract_or_fixed_term"}
            else "standard"
        )
        grouped[section].append(job)

    lines: list[str] = []
    included: list[Job] = []
    stop = False
    for section, header in sections:
        section_started = False
        for job in grouped[section]:
            if len(included) >= max_items:
                stop = True
                break
            additions = [header, _digest_line(job)] if not section_started else [_digest_line(job)]
            candidate = "\n\n".join([*lines, *additions])
            if len(candidate) > max_description_chars:
                stop = True
                break
            lines.extend(additions)
            included.append(job)
            section_started = True
        if stop:
            break
    label = "Explore" if delivery_kind == "explore" else "Digest"
    return GroupedJobPayload(
        title=(
            f"{'🔎' if delivery_kind == 'explore' else '📋'} {label} — "
            f"{len(included)} pending job{'s' if len(included) != 1 else ''}"
        ),
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
    policy: NotificationPolicy | None = None,
) -> DeliveryRunResult:
    """Retry every configured immediate destination from durable pending state."""

    async with immediate_delivery_lease():
        return await _process_pending_immediate_deliveries_unlocked(
            discord_notifier=discord_notifier,
            telegram_notifier=telegram_notifier,
            policy=policy,
        )


async def _process_pending_immediate_deliveries_unlocked(
    *,
    discord_notifier: DiscordNotifier | None = None,
    telegram_notifier: TelegramNotifier | None = None,
    policy: NotificationPolicy | None = None,
) -> DeliveryRunResult:
    """Perform one leased pending-immediate obligation attempt."""

    discord = discord_notifier or DiscordNotifier()
    telegram = telegram_notifier or TelegramNotifier()
    notification_policy = policy or load_notification_policy()
    selected = 0
    successes: list[DeliverySuccess] = []

    for destination in ("discord_general", "discord_ngo"):
        if not discord.has_destination(destination):
            continue
        jobs = await get_pending_delivery_jobs(
            "immediate",
            destination,
            limit=notification_policy.immediate_max_items,
            max_age_days=notification_policy.pending_max_age_days,
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
            limit=notification_policy.immediate_max_items,
            max_age_days=notification_policy.pending_max_age_days,
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
    policy: NotificationPolicy | None = None,
) -> DeliveryRunResult:
    """Deliver one Discord-general digest batch and receipt exact membership."""

    discord = discord_notifier or DiscordNotifier()
    notification_policy = policy or load_notification_policy()
    if not discord.general_configured:
        return DeliveryRunResult()

    jobs = await get_pending_delivery_jobs(
        "digest",
        "discord_general",
        limit=notification_policy.digest_max_items,
        max_age_days=notification_policy.pending_max_age_days,
        ngo_webhook_configured=discord.ngo_configured,
    )
    payload = build_digest_payload(
        jobs,
        max_items=notification_policy.digest_max_items,
    )
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


async def process_pending_explore_delivery(
    *,
    total_jobs: int,
    discord_notifier: DiscordNotifier | None = None,
    policy: NotificationPolicy | None = None,
) -> DeliveryRunResult:
    """Deliver one Discord-general explore batch and receipt exact membership."""

    discord = discord_notifier or DiscordNotifier()
    notification_policy = policy or load_notification_policy()
    if not notification_policy.daily_explore_enabled or not discord.general_configured:
        return DeliveryRunResult()

    jobs = await get_pending_delivery_jobs(
        "explore",
        "discord_general",
        limit=notification_policy.explore_max_items,
        max_age_days=notification_policy.pending_max_age_days,
        ngo_webhook_configured=discord.ngo_configured,
    )
    payload = build_explore_payload(
        jobs,
        max_items=notification_policy.explore_max_items,
    )
    if not payload.jobs:
        return DeliveryRunResult(selected_count=len(jobs))

    try:
        successes = await discord.send_grouped_digest(payload, total_jobs=total_jobs)
    except Exception:
        logger.exception(
            "Explore delivery failed; leaving {} included job(s) pending",
            len(payload.jobs),
        )
        successes = []
    await record_delivery_receipts("explore", list(successes))
    logger.info(
        "Explore selected {}, included {}, delivered {} job(s)",
        len(jobs),
        len(payload.jobs),
        len(successes),
    )
    return DeliveryRunResult(
        selected_count=len(jobs),
        included_count=len(payload.jobs),
        successes=tuple(successes),
    )
