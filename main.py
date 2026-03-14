"""Job Tracker Bot — entry point.

Supports:
  python main.py --dry-run              # one scan, print results, no DB/notifications
  python main.py --dry-run --source remotive   # test a single source
  python main.py --dry-run --verbose    # show rejected jobs with reasons
  python main.py --stats                # show database statistics
  python main.py                        # full scheduler mode (APScheduler)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

import config
from filters.language import passes_language_filter
from filters.location import classify_remote_scope, passes_location_filter
from filters.match import compute_match_score
from filters.ngo import classify_ngo
from filters.role import passes_role_filter
from models.job import Job
from notifiers.discord_notifier import DiscordNotifier
from notifiers.telegram_notifier import TelegramNotifier
from sources.arbeitnow import ArbeitnowSource
from sources.idealist import IdealistSource
from sources.reliefweb import ReliefWebSource
from sources.remoteok import RemoteOKSource
from sources.remotive import RemotiveSource
from sources.weworkremotely import WeWorkRemotelySource
from storage.database import (
    filter_unseen,
    get_recent_unnotified,
    get_stats,
    get_total_count,
    init_db,
    save_jobs,
)

# ── Logging setup ──────────────────────────────────────────────────────────
logger.remove()  # remove default stderr handler
logger.add(sys.stderr, level=config.LOG_LEVEL, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
log_dir = Path(config.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)
logger.add(config.LOG_FILE, level="DEBUG", rotation="10 MB", retention="7 days")

# Max jobs per company in a single scan to avoid one employer flooding results
_MAX_JOBS_PER_COMPANY = 2

# ── Source registry ────────────────────────────────────────────────────────
ALL_SOURCES = {
    "remotive": RemotiveSource,
    "arbeitnow": ArbeitnowSource,
    "remoteok": RemoteOKSource,
    "weworkremotely": WeWorkRemotelySource,
    "idealist": IdealistSource,
    "reliefweb": ReliefWebSource,
}


def _get_sources(source_name: str | None) -> list:
    """Return source instances to run — all or a single one."""
    if source_name:
        cls = ALL_SOURCES.get(source_name)
        if cls is None:
            logger.error("Unknown source '{}'. Available: {}", source_name, list(ALL_SOURCES.keys()))
            sys.exit(1)
        return [cls()]
    return [cls() for cls in ALL_SOURCES.values()]


def _apply_filters(
    jobs: list[Job],
    max_age_days: int | None = None,
    verbose: bool = False,
) -> list[Job]:
    """Run all filters on a list of jobs. Returns only accepted jobs.

    Also performs:
      - In-memory content-hash dedup
      - Per-company cap (max 2 per scan)
      - Arbeitnow on-site rejection (is_remote=False + germany scope)
      - Arbeitnow unknown→germany scope defaulting

    When *verbose* is True, prints rejection reasons to stdout (for debugging).
    """
    accepted: list[Job] = []
    rejected: list[tuple[Job, str]] = []  # (job, reason) for verbose output
    seen_content_hashes: set[str] = set()
    company_counts: defaultdict[str, int] = defaultdict(int)

    # Sort by posted_at descending so per-company cap keeps most recent
    def _sort_key(j: Job) -> datetime:
        dt = j.posted_at or j.fetched_at
        # Normalize to UTC-aware to avoid naive vs aware comparison errors
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    jobs_sorted = sorted(jobs, key=_sort_key, reverse=True)

    for job in jobs_sorted:
        # Helper to record a rejection reason for verbose output
        def _reject(job: Job, reason: str) -> None:
            if verbose:
                rejected.append((job, reason))

        # 0. In-memory content dedup (title+company+location)
        if job.content_hash in seen_content_hashes:
            logger.debug("Dedup SKIP (in-memory): {}", job.title)
            _reject(job, "dedup (content hash)")
            continue

        # 1. Classify remote scope (enrichment, not a filter)
        #    If the source already set a meaningful scope (e.g. Idealist
        #    pre-classifies from Algolia's remoteZone, RemoteOK pre-parses
        #    location), keep it.
        if job.remote_scope not in ("worldwide", "eu", "germany", "restricted"):
            job.remote_scope = classify_remote_scope(job)

        # 1b. Arbeitnow default: unknown scope → "germany"
        #     Arbeitnow is a Germany-focused board, safe assumption.
        if job.source == "arbeitnow" and job.remote_scope == "unknown":
            job.remote_scope = "germany"

        # 1c. Remote-only board default: unknown scope → "worldwide"
        #     For WeWorkRemotely and Remotive, if scope is still unknown
        #     after classification, they're remote-only boards so benefit
        #     of the doubt → worldwide.
        if job.source in ("weworkremotely", "remotive") and job.remote_scope == "unknown":
            job.remote_scope = "worldwide"

        # 1c. Arbeitnow on-site rejection: if the API says is_remote=False
        #     and the scope is "germany", the job is on-site only — reject.
        if job.source == "arbeitnow" and not job.is_remote and job.remote_scope == "germany":
            logger.debug("Location REJECT (arbeitnow on-site): {}", job.title)
            _reject(job, "location: arbeitnow on-site (germany, not remote)")
            continue

        # 2. Location filter
        if not passes_location_filter(job):
            _reject(job, f"location: scope={job.remote_scope}, loc='{job.location}'")
            continue

        # 3. Role filter
        if not passes_role_filter(job):
            _reject(job, f"role: no dev keyword in title '{job.title}'")
            continue

        # 4. Language filter
        if not passes_language_filter(job):
            _reject(job, "language: non-English content detected")
            continue

        # 5. Recency filter — reject jobs older than max_age_days
        #    Per-source override: check SOURCE_MAX_AGE_DAYS first
        source_max = config.SOURCE_MAX_AGE_DAYS.get(job.source)
        if source_max is not None:
            effective_max_age = source_max
        elif max_age_days is not None:
            effective_max_age = max_age_days
        else:
            effective_max_age = config.MAX_JOB_AGE_DAYS
        if job.posted_at is not None:
            posted = job.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - posted
            if age > timedelta(days=effective_max_age):
                age_days = age.total_seconds() / 86400
                logger.debug(
                    "Recency REJECT ({:.0f}d old, max {}d): {}",
                    age_days, effective_max_age, job.title,
                )
                _reject(job, f"recency: {age_days:.0f}d old (max {effective_max_age}d)")
                continue

        # 6. NGO classification (enrichment — never rejects)
        classify_ngo(job)

        # 6b. Match score (enrichment — never rejects)
        job.match_score = compute_match_score(job)

        # 7. Per-company cap
        company_key = job.company.lower().strip()
        if company_counts[company_key] >= _MAX_JOBS_PER_COMPANY:
            logger.warning(
                "Company cap ({}) reached for '{}' — skipping: {}",
                _MAX_JOBS_PER_COMPANY, job.company, job.title,
            )
            _reject(job, f"company cap: already {_MAX_JOBS_PER_COMPANY} from '{job.company}'")
            continue

        company_counts[company_key] += 1
        seen_content_hashes.add(job.content_hash)
        accepted.append(job)

    logger.info("Filters: {} in → {} accepted", len(jobs), len(accepted))

    # Sort by match_score DESC so highest-match jobs appear first
    accepted.sort(key=lambda j: j.match_score, reverse=True)

    # Print verbose rejection table when requested
    if verbose and rejected:
        _print_rejections(rejected)

    return accepted


def _print_rejections(rejected: list[tuple[Job, str]]) -> None:
    """Print a human-readable table of rejected jobs with reasons."""
    print(f"\n{'='*78}")
    print(f"  REJECTED JOBS: {len(rejected)} total")
    print(f"{'='*78}\n")

    # Group by rejection reason category
    by_reason: defaultdict[str, list[Job]] = defaultdict(list)
    for job, reason in rejected:
        category = reason.split(":")[0].strip()
        by_reason[category].append(job)

    for category, count in sorted(
        ((k, len(v)) for k, v in by_reason.items()),
        key=lambda x: -x[1],
    ):
        print(f"  ── {category.upper()} ({count}) {'─'*50}")

    print()

    for i, (job, reason) in enumerate(rejected, 1):
        age_str = _format_age(job.posted_at)
        print(f"  ❌ [{i}] {job.title}")
        print(f"      🏢  {job.company}")
        print(f"      📍  {job.location} (scope={job.remote_scope or 'unknown'})")
        print(f"      📅  {age_str}  |  🌍  {job.source}")
        print(f"      ⛔  Reason: {reason}")
        print()


async def run_scan(
    sources: list,
    dry_run: bool = False,
    max_age_days: int | None = None,
    verbose: bool = False,
) -> list[Job]:
    """Fetch from all sources, filter, deduplicate, and optionally persist."""

    # Fetch from all sources concurrently
    fetch_tasks = [src.safe_fetch() for src in sources]
    results = await asyncio.gather(*fetch_tasks)

    # Flatten
    all_jobs: list[Job] = []
    for batch in results:
        all_jobs.extend(batch)

    logger.info("Total raw jobs fetched: {}", len(all_jobs))

    # Apply filters
    filtered = _apply_filters(all_jobs, max_age_days=max_age_days, verbose=verbose)

    if dry_run:
        # Print results and exit — don't touch DB or send notifications
        _print_jobs(filtered)
        return filtered

    # Deduplicate against DB
    await init_db()
    new_jobs = await filter_unseen(filtered)

    if new_jobs:
        await save_jobs(new_jobs)
        logger.info("{} new jobs saved to database", len(new_jobs))

        # Send Discord notifications
        await _send_notifications(new_jobs)
    else:
        logger.info("No new jobs this cycle")

    return new_jobs


async def _send_notifications(jobs: list[Job]) -> None:
    """Send notifications for new jobs via all configured channels."""
    # Discord
    if config.DISCORD_WEBHOOK_URL:
        notifier = DiscordNotifier()
        await notifier.send_jobs(jobs)
    else:
        logger.warning("Discord webhook URL not configured — skipping notifications")

    # Telegram
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        notifier = TelegramNotifier()
        await notifier.send_jobs(jobs)
    else:
        logger.debug("Telegram not configured — skipping")


def _format_age(posted_at: datetime | None) -> str:
    """Human-readable age string for dry-run display."""
    if posted_at is None:
        return "age unknown"
    dt = posted_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days < 0:
        return "posted today"
    if days == 0:
        hours = int(delta.total_seconds() // 3600)
        if hours == 0:
            return "just now"
        return f"{hours}h ago"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def _print_jobs(jobs: list[Job]) -> None:
    """Pretty-print jobs to stdout (for --dry-run)."""
    if not jobs:
        print("\n  No jobs matched your filters.\n")
        return

    ngo_jobs = [j for j in jobs if j.is_ngo]
    general_jobs = [j for j in jobs if not j.is_ngo]

    print(f"\n{'='*70}")
    print(f"  DRY RUN RESULTS: {len(jobs)} jobs matched")
    print(f"  🟢 NGO/nonprofit: {len(ngo_jobs)}  |  🔵 General: {len(general_jobs)}")
    print(f"{'='*70}\n")

    for i, job in enumerate(jobs, 1):
        icon = "🟢" if job.is_ngo else "🔵"
        age_str = _format_age(job.posted_at)
        print(f"  {icon} [{i}] {job.title}")
        print(f"      🏢  {job.company}")
        print(f"      📍  {job.location} ({job.remote_scope or 'unknown'})")
        if job.match_score > 0:
            from filters.match import match_score_bar
            bar = match_score_bar(job.match_score)
            print(f"      📊  {bar}  {job.match_score}% match")
        if job.salary:
            print(f"      💰  {job.salary}")
        if job.tags:
            print(f"      🏷️   {', '.join(job.tags[:5])}")
        print(f"      🌍  Source: {job.source}  |  📅  {age_str}")
        print(f"      🔗  {job.url}")
        print()


async def _show_stats() -> None:
    """Query the database and print a summary dashboard."""
    await init_db()
    stats = await get_stats()

    total = stats["total"]
    ngo_count = stats["ngo_count"]
    new_24h = stats["new_24h"]
    sources = stats["sources"]
    top_companies = stats["top_companies"]
    last_fetched = stats["last_fetched_at"]

    # Last scan age
    if last_fetched:
        dt = last_fetched
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            last_scan_str = "just now"
        elif minutes < 60:
            last_scan_str = f"{minutes} minutes ago"
        elif minutes < 1440:
            hours = minutes // 60
            last_scan_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = minutes // 1440
            last_scan_str = f"{days} day{'s' if days != 1 else ''} ago"
    else:
        last_scan_str = "never"

    print(f"\n{'='*60}")
    print(f"  📊  JOB TRACKER — DATABASE STATS")
    print(f"{'='*60}\n")

    print(f"  Total jobs in DB:    {total}")
    print(f"  New in last 24h:     {new_24h}")
    print(f"  NGO / nonprofit:     {ngo_count}")
    print(f"  General:             {total - ngo_count}")
    print(f"  Last scan:           {last_scan_str}")

    if sources:
        print(f"\n  {'─'*50}")
        print(f"  📡  Sources breakdown:")
        for src, count in sources.items():
            bar = "█" * min(count, 40)
            print(f"      {src:<20s} {count:>4d}  {bar}")

    if top_companies:
        print(f"\n  {'─'*50}")
        print(f"  🏢  Top companies:")
        for company, count in top_companies:
            name = company if len(company) <= 35 else company[:32] + "..."
            print(f"      {name:<35s} ({count})")

    print(f"\n{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Job Tracker Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run one scan cycle and print results without saving to DB or sending notifications.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help=f"Test a single source. Options: {', '.join(ALL_SOURCES.keys())}",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=None,
        metavar="DAYS",
        help=f"Max job age in days (default: {config.MAX_JOB_AGE_DAYS} from MAX_JOB_AGE_DAYS env var).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show rejected jobs with reasons during --dry-run (debug mode).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show database statistics and exit.",
    )
    args = parser.parse_args()

    # ── Stats mode — query DB and print summary ───────────────────────
    if args.stats:
        asyncio.run(_show_stats())
        return

    sources = _get_sources(args.source)
    source_names = [s.name for s in sources]

    if args.dry_run:
        # One-shot scan — no scheduler
        max_age = args.max_age  # None means use config default
        logger.info(
            "Starting DRY RUN scan — sources: {}, max age: {}d{}",
            source_names, max_age or config.MAX_JOB_AGE_DAYS,
            " (verbose)" if args.verbose else "",
        )
        asyncio.run(run_scan(sources, dry_run=True, max_age_days=max_age, verbose=args.verbose))
        return

    # ── Full scheduler mode ────────────────────────────────────────────
    logger.info("Starting Job Tracker Bot in scheduler mode")
    logger.info(
        "Scan every {} min | Digest every {} h | Health check every 1 h",
        config.SCAN_INTERVAL_MINUTES,
        config.DIGEST_INTERVAL_HOURS,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Initialize DB before scheduling
    loop.run_until_complete(init_db())

    scheduler = AsyncIOScheduler(event_loop=loop)

    # 1. Main job scan — every N minutes
    scheduler.add_job(
        _scheduled_scan,
        "interval",
        minutes=config.SCAN_INTERVAL_MINUTES,
        id="scan",
        name="Job Scan",
        next_run_time=datetime.now(timezone.utc),  # run immediately on start
    )

    # 2. Digest summary — every N hours
    scheduler.add_job(
        _scheduled_digest,
        "interval",
        hours=config.DIGEST_INTERVAL_HOURS,
        id="digest",
        name="Digest Summary",
    )

    # 3. Health check — every hour
    scheduler.add_job(
        _scheduled_health_check,
        "interval",
        hours=1,
        id="health",
        name="Health Check",
    )

    scheduler.start()
    logger.info("Scheduler started — press Ctrl+C to stop")

    # ── Discord bot (optional — runs if DISCORD_BOT_TOKEN is set) ──────
    discord_bot = None
    if config.DISCORD_BOT_TOKEN and config.DISCORD_COMMAND_CHANNEL_ID:
        from discord_bot import JobTrackerBot

        async def _manual_scan_callback():
            """Callback for the Discord bot's scan command."""
            sources_list = _get_sources(None)
            return await run_scan(sources_list, dry_run=False)

        channel_id = int(config.DISCORD_COMMAND_CHANNEL_ID)
        discord_bot = JobTrackerBot(
            command_channel_id=channel_id,
            scan_callback=_manual_scan_callback,
        )

        # Update scan timing info for the stats command
        scan_job = scheduler.get_job("scan")
        if scan_job:
            discord_bot.set_scan_times(
                last_scan=None,
                next_scan=scan_job.next_run_time,
            )

        # Run Discord bot in the same event loop
        loop.create_task(discord_bot.start(config.DISCORD_BOT_TOKEN))
        logger.info("Discord bot starting (channel: {})", config.DISCORD_COMMAND_CHANNEL_ID)
    else:
        logger.info("Discord bot not configured (set DISCORD_BOT_TOKEN and DISCORD_COMMAND_CHANNEL_ID)")

    # Graceful shutdown on SIGINT / SIGTERM
    def _shutdown(sig, frame):
        logger.info("Received signal {} — shutting down...", sig)
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if discord_bot:
            loop.create_task(discord_bot.close())
        loop.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if discord_bot and not discord_bot.is_closed():
            loop.run_until_complete(discord_bot.close())
        loop.close()
        logger.info("Job Tracker Bot stopped")


# ── Scheduled tasks ────────────────────────────────────────────────────────

async def _scheduled_scan() -> None:
    """Scheduled scan task — runs all sources."""
    logger.info("⏰ Scheduled scan starting...")
    sources = _get_sources(None)
    try:
        await run_scan(sources, dry_run=False)
    except Exception:
        logger.exception("Scheduled scan failed")


async def _scheduled_digest() -> None:
    """Send a digest summary of recent unnotified jobs."""
    try:
        recent = await get_recent_unnotified(hours=config.DIGEST_INTERVAL_HOURS)
        total = await get_total_count()

        if recent:
            logger.info(
                "📋 Digest: {} unnotified jobs in last {} hours (total in DB: {})",
                len(recent), config.DIGEST_INTERVAL_HOURS, total,
            )
            # Send digest via Discord as a summary message
            if config.DISCORD_WEBHOOK_URL:
                from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

                webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
                embed = DiscordEmbed(
                    title=f"📋 Digest — {len(recent)} jobs in last {config.DIGEST_INTERVAL_HOURS}h",
                    description="\n".join(
                        f"• **{r['title']}** at {r['company']} ({r['source']})"
                        for r in recent[:20]  # cap at 20 to avoid embed limits
                    ),
                    color=0x9B59B6,  # purple for digest
                )
                embed.set_footer(text=f"Total jobs tracked: {total}")
                embed.set_timestamp(datetime.now(timezone.utc).isoformat())
                webhook.add_embed(embed)
                await webhook.execute()
                logger.info("📋 Digest sent to Discord")
        else:
            logger.info(
                "📋 Digest: no unnotified jobs in last {} hours (total in DB: {})",
                config.DIGEST_INTERVAL_HOURS, total,
            )
    except Exception:
        logger.exception("Digest task failed")


async def _scheduled_health_check() -> None:
    """Log a health-check message."""
    try:
        total = await get_total_count()
        logger.info("💚 Health check — bot is alive, {} jobs tracked so far", total)
    except Exception:
        logger.exception("Health check failed")


if __name__ == "__main__":
    main()
